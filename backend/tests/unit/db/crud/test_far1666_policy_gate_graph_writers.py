"""FAR-1666: graph writers that bypassed policy-gate reconciliation.

FAR-1109 moved policy-gate reconciliation into
``db/crud/pipeline.py::replace_pipeline_graph``, but three other writers assign
``graph_nodes_json`` directly. This module pins the per-site treatment decided
for the ticket:

* the snapshot-rollback writer **TRANSLATES** — it calls the SAME shared
  ``reconcile_graph_policy_gates`` with ``previous=current`` and
  ``new=snapshot`` nodes, so a gate a rollback reintroduces gets its row and a
  gate a rollback drops is soft-deleted;
* the clone writer **STRIPS** declared blocks (evals/gates are not cloned) with
  a loud, recorded warning — never a silent drop.

The tests run the REAL CRUD functions against a real (in-memory SQLite) session
and read the ``policy_gates`` rows back where a row lifecycle is the subject, or
assert the disclosed warning where a strip is the subject. Postgres-only
advisory-lock/timeout statements are bypassed at the session boundary so the
surrounding Python still executes on SQLite.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.db.crud.pipeline import (
    _clone_pipeline_config,
    _clone_snapshots,
    _CloneSourceSnapshot,
    strip_declared_policy_gates,
)
from modulo.db.crud.pipeline_snapshot_versioning import rollback_to_snapshot
from modulo.db.models.account import Account
from modulo.db.models.base import Base
from modulo.db.models.eval import Eval
from modulo.db.models.org_membership import OrgMembership
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.pipeline_snapshot import PipelineSnapshot
from modulo.db.models.policy_gate import PolicyGate
from modulo.db.soft_delete import include_soft_deleted

# Scoped create_all: unrelated models use Postgres-only column types that SQLite
# cannot render (same rationale as tests/unit/db/test_seed_demo.py).
_TABLE_NAMES = {
    "accounts",
    "org_memberships",
    "pipelines",
    "pipeline_edges",
    "pipeline_snapshots",
    "audit_events",
    "audit_chain_heads",
    "evals",
    "policy_gates",
    # FAR-1593: the clone seeds the org's applied guardrail config in the
    # caller's transaction, which reads organisations.guardrail_pins_json — the
    # table must exist for the real CRUD to run (an empty table seeds nothing,
    # which is exactly this module's case: no applied guardrail config).
    "organisations",
}

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-00000000000a")
_ACCOUNT_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
_PIPELINE_ID = uuid.UUID("00000000-0000-0000-0000-0000000000f1")
_EVAL_ID = uuid.UUID("660e8400-e29b-41d4-a716-446655440001")

_NODE_A = "00000000-0000-0000-0000-0000000000a1"
_NODE_B = "00000000-0000-0000-0000-0000000000a2"

# The statements the translation issues are Postgres-only (pg_advisory_xact_lock
# / hashtext / SET LOCAL statement_timeout / RESET statement_timeout). Intercept
# them at the session boundary so the surrounding Python runs on SQLite.
_PG_ONLY_MARKERS = ("statement_timeout", "pg_advisory_xact_lock", "hashtext")


def _bypass_postgres_only_statements(session: AsyncSession) -> AsyncSession:
    real_execute = session.execute

    async def _execute(statement: Any, *args: Any, **kwargs: Any) -> Any:
        sql = str(statement)
        if any(marker in sql for marker in _PG_ONLY_MARKERS):
            return MagicMock()
        return await real_execute(statement, *args, **kwargs)

    session.execute = _execute
    return session


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
        yield _bypass_postgres_only_statements(s)


def _plain_node(node_id: str) -> dict[str, Any]:
    return {"id": node_id, "node_type": "agent", "position": {"x": 0, "y": 0}, "label": node_id[-2:]}


def _gate_node(node_id: str = _NODE_A, eval_id: str = str(_EVAL_ID), action: str = "block") -> dict[str, Any]:
    node = _plain_node(node_id)
    node["policy_gate"] = {"eval_id": eval_id, "action": action}
    return node


def _clone_snapshot(nodes: list[dict[str, Any]]) -> _CloneSourceSnapshot:
    return _CloneSourceSnapshot(
        name="Source",
        description=None,
        visibility="org",
        owner_team_id=None,
        max_concurrent_runs=5,
        lock_wait_timeout_seconds=300,
        node_timeout_seconds=300,
        hitl_review_window_seconds=None,
        run_context_defaults={},
        graph_nodes_json=nodes,
        default_autonomy_level="manual_approval",
        max_autonomy_level=None,
        stale_run_timeout_minutes=60,
        stdout_retention_config=None,
        edges=[],
        snapshots=[],
    )


def _clone_source_snapshot_row(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    """A plain-data snapshot row as ``_snapshot_to_dict`` produces for the clone."""
    return {
        "snapshot_version": 1,
        "account_id": _ACCOUNT_ID,
        "environment_profile_id": None,
        "graph_json": {"nodes": nodes, "edges": []},
        "connector_bindings_json": [],
        "schema_pins_json": [],
        "prompt_pins_json": [],
        "model_backend_pins_json": [],
        "composite_bindings_json": None,
        "parameter_bindings_json": None,
        "guardrail_pins_json": None,
        "guardrail_pins_fingerprint": None,
        "policy_gate_pins_json": None,
        "policy_gate_pins_fingerprint": None,
        "tag": None,
        "notes": None,
        "default_autonomy_level": "manual_approval",
        "max_autonomy_level": None,
        "config_json": {},
        "run_context_defaults": {},
        "stdout_retention_config": None,
        "pins": [],
    }


async def _seed_eval(
    session: AsyncSession,
    *,
    node_id: str = _NODE_A,
    eval_id: uuid.UUID = _EVAL_ID,
) -> Eval:
    eval_row = Eval(
        id=eval_id,
        organisation_id=_ORG_ID,
        pipeline_id=_PIPELINE_ID,
        node_id=uuid.UUID(node_id),
        account_id=_ACCOUNT_ID,
        eval_type="llm_judge",
        config_json={},
        version=1,
    )
    session.add(eval_row)
    await session.flush()
    return eval_row


async def _live_gates(session: AsyncSession) -> list[PolicyGate]:
    # State the soft-delete predicate EXPLICITLY instead of depending on the
    # app-wide soft-delete filter, which is an ORM event listener registered
    # only by ``modulo.db.session`` — a module this test's import chain never
    # loads. Expire first so a row mutated in-session (the fixture commits with
    # ``expire_on_commit=False``) is re-read against the server's view before
    # the filtered select.
    session.expire_all()
    stmt = select(PolicyGate).where(PolicyGate.deleted_at.is_(None))
    return list((await session.execute(stmt)).scalars())


class TestStripDeclaredPolicyGates:
    def test_removes_blocks_and_reports_each_drop(self) -> None:
        nodes = [_gate_node(_NODE_A), _plain_node(_NODE_B)]
        stripped, dropped = strip_declared_policy_gates(nodes)

        assert "policy_gate" not in stripped[0]
        assert stripped[1] is nodes[1]
        assert dropped == [f"node {_NODE_A} (eval {_EVAL_ID})"]

    def test_never_mutates_the_caller_nodes(self) -> None:
        original = _gate_node(_NODE_A)
        nodes = [original]
        stripped, dropped = strip_declared_policy_gates(nodes)

        assert "policy_gate" in original
        assert "policy_gate" not in stripped[0]
        assert dropped

    def test_gate_free_graph_is_returned_unchanged(self) -> None:
        nodes = [_plain_node(_NODE_A), _plain_node(_NODE_B)]
        stripped, dropped = strip_declared_policy_gates(nodes)

        assert not dropped
        assert stripped == nodes

    def test_none_and_non_dict_nodes_are_tolerated(self) -> None:
        nodes: list[Any] = [None, "not-a-dict", _gate_node(_NODE_A)]
        stripped, dropped = strip_declared_policy_gates(nodes)

        assert stripped[0] is None
        assert stripped[1] == "not-a-dict"
        assert dropped == [f"node {_NODE_A} (eval {_EVAL_ID})"]

    def test_none_list_yields_empty(self) -> None:
        stripped, dropped = strip_declared_policy_gates(None)
        assert not stripped
        assert not dropped

    def test_malformed_gate_block_is_still_stripped(self) -> None:
        node = _plain_node(_NODE_A)
        node["policy_gate"] = "not-a-dict"
        stripped, dropped = strip_declared_policy_gates([node])

        assert "policy_gate" not in stripped[0]
        assert dropped == [f"node {_NODE_A} (eval None)"]


class TestCloneStripsDeclaredPolicyGates:
    async def test_clone_nodes_carry_no_declaration_and_warn(
        self,
        session: AsyncSession,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="modulo.db.crud.pipeline"):
            cloned = await _clone_pipeline_config(
                session,
                _clone_snapshot([_gate_node(_NODE_A), _plain_node(_NODE_B)]),
                org_id=_ORG_ID,
                account_id=_ACCOUNT_ID,
                pipeline_id=_PIPELINE_ID,
                new_name=None,
            )

        nodes = list(cloned.graph_nodes_json)
        assert all("policy_gate" not in node for node in nodes)
        assert "policy_gate declaration(s) dropped on clone" in caplog.text
        assert _NODE_A in caplog.text

    async def test_gate_free_clone_does_not_warn(
        self,
        session: AsyncSession,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="modulo.db.crud.pipeline"):
            await _clone_pipeline_config(
                session,
                _clone_snapshot([_plain_node(_NODE_A)]),
                org_id=_ORG_ID,
                account_id=_ACCOUNT_ID,
                pipeline_id=_PIPELINE_ID,
                new_name=None,
            )

        assert "policy_gate declaration(s) dropped on clone" not in caplog.text


class TestCloneSnapshotsStripDeclaredPolicyGates:
    async def test_cloned_snapshot_nodes_carry_no_declaration_and_warn(
        self,
        session: AsyncSession,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        source_nodes = [_gate_node(_NODE_A), _plain_node(_NODE_B)]
        row = _clone_source_snapshot_row(source_nodes)
        cloned_id = uuid.uuid4()

        with caplog.at_level(logging.WARNING, logger="modulo.db.crud.pipeline"):
            count = await _clone_snapshots(
                session,
                [row],
                source_id=_PIPELINE_ID,
                cloned_id=cloned_id,
                org_id=_ORG_ID,
            )

        assert count == 1
        stored = (
            await session.execute(select(PipelineSnapshot).where(PipelineSnapshot.pipeline_id == cloned_id))
        ).scalar_one()
        assert all("policy_gate" not in node for node in stored.graph_json["nodes"])
        assert "policy_gate declaration(s) dropped on clone of snapshot" in caplog.text
        assert _NODE_A in caplog.text
        # The source dict is deep-copied, never mutated: a re-clone (or a
        # rollback path reading the source) must still see the declaration.
        assert "policy_gate" in source_nodes[0]

    async def test_gate_free_snapshot_does_not_warn(
        self,
        session: AsyncSession,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        row = _clone_source_snapshot_row([_plain_node(_NODE_A)])

        with caplog.at_level(logging.WARNING, logger="modulo.db.crud.pipeline"):
            await _clone_snapshots(
                session,
                [row],
                source_id=_PIPELINE_ID,
                cloned_id=uuid.uuid4(),
                org_id=_ORG_ID,
            )

        assert "policy_gate declaration(s) dropped on clone of snapshot" not in caplog.text

    async def test_non_dict_graph_json_is_copied_unchanged_without_warning(
        self,
        session: AsyncSession,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # A malformed (non-dict) graph_json is a JSON value the column permits,
        # so the defensive ``isinstance(graph_json, dict)`` guard must fall
        # through: the value is deep-copied and stored as-is, and the strip
        # path (and its warning) is never reached.
        row = _clone_source_snapshot_row([_plain_node(_NODE_A)])
        row["graph_json"] = ["not-a-dict"]
        cloned_id = uuid.uuid4()

        with caplog.at_level(logging.WARNING, logger="modulo.db.crud.pipeline"):
            count = await _clone_snapshots(
                session,
                [row],
                source_id=_PIPELINE_ID,
                cloned_id=cloned_id,
                org_id=_ORG_ID,
            )

        assert count == 1
        stored = (
            await session.execute(select(PipelineSnapshot).where(PipelineSnapshot.pipeline_id == cloned_id))
        ).scalar_one()
        assert stored.graph_json == ["not-a-dict"]
        assert "policy_gate declaration(s) dropped on clone of snapshot" not in caplog.text


class TestRollbackReconcilesDeclaredPolicyGates:
    async def _seed_rollback_scene(
        self,
        session: AsyncSession,
        *,
        live_nodes: list[dict[str, Any]],
        snapshot_nodes: list[dict[str, Any]],
    ) -> PipelineSnapshot:
        session.add(Account(id=_ACCOUNT_ID, email="rollback@example.com", display_name="Rollback Actor"))
        session.add(OrgMembership(account_id=_ACCOUNT_ID, organisation_id=_ORG_ID, role="admin"))
        session.add(
            Pipeline(
                id=_PIPELINE_ID,
                organisation_id=_ORG_ID,
                account_id=_ACCOUNT_ID,
                name="Rollback Pipeline",
                graph_nodes_json=live_nodes,
            )
        )
        snapshot = PipelineSnapshot(
            id=uuid.uuid4(),
            organisation_id=_ORG_ID,
            pipeline_id=_PIPELINE_ID,
            snapshot_version=1,
            graph_json={"nodes": snapshot_nodes, "edges": []},
            connector_bindings_json=[],
            schema_pins_json=[],
            prompt_pins_json=[],
            model_backend_pins_json=[],
            config_json={},
            run_context_defaults={},
        )
        session.add(snapshot)
        await session.flush()
        return snapshot

    async def _rollback(self, session: AsyncSession, snapshot_id: uuid.UUID) -> None:
        # ``create_snapshot_from_live_graph`` takes a Postgres advisory lock
        # SQLite cannot execute; it runs AFTER the reconciliation under test.
        with patch(
            "modulo.db.crud.pipeline_snapshot_versioning.create_snapshot_from_live_graph",
            new=AsyncMock(return_value=None),
        ):
            result = await rollback_to_snapshot(
                session,
                _PIPELINE_ID,
                snapshot_id,
                account_id=_ACCOUNT_ID,
                is_privileged=False,
                caller_type="mcp",
                is_guardrail_admin=True,
            )
        assert result is None
        await session.flush()

    async def test_rollback_reintroducing_a_gate_creates_its_row(self, session: AsyncSession) -> None:
        await _seed_eval(session)
        snapshot = await self._seed_rollback_scene(
            session,
            live_nodes=[_plain_node(_NODE_A)],
            snapshot_nodes=[_gate_node(_NODE_A)],
        )

        await self._rollback(session, snapshot.id)

        live = await _live_gates(session)
        assert len(live) == 1
        assert live[0].eval_id == _EVAL_ID
        assert live[0].action == "block"
        assert live[0].deleted_at is None

    async def test_rollback_dropping_a_gate_soft_deletes_its_row(self, session: AsyncSession) -> None:
        await _seed_eval(session)
        session.add(
            PolicyGate(
                organisation_id=_ORG_ID,
                eval_id=_EVAL_ID,
                node_id=uuid.UUID(_NODE_A),
                action="block",
                version=1,
                enabled=True,
                enabled_at=datetime.now(UTC),
            )
        )
        snapshot = await self._seed_rollback_scene(
            session,
            live_nodes=[_gate_node(_NODE_A)],
            snapshot_nodes=[_plain_node(_NODE_A)],
        )

        await self._rollback(session, snapshot.id)

        rows = list((await session.execute(include_soft_deleted(select(PolicyGate)))).scalars())
        assert len(rows) == 1
        assert rows[0].deleted_at is not None
        assert not await _live_gates(session)

    async def test_rollback_invokes_the_shared_reconciliation(self, session: AsyncSession) -> None:
        snapshot = await self._seed_rollback_scene(
            session,
            live_nodes=[_plain_node(_NODE_A)],
            snapshot_nodes=[_plain_node(_NODE_B)],
        )

        with patch(
            "modulo.db.crud.pipeline_snapshot_versioning.reconcile_graph_policy_gates",
            new=AsyncMock(),
        ) as reconcile:
            await self._rollback(session, snapshot.id)

        reconcile.assert_awaited_once()
