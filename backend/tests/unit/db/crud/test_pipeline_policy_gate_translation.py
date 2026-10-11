"""FAR-1109: node-attached policy-gate translation in the graph-write transaction.

``reconcile_graph_policy_gates`` translates a pipeline graph's declared
``policy_gate`` blocks into the ``policy_gates`` row lifecycle (create / update
in place / soft-delete), and ``replace_pipeline_graph`` calls it inside the one
shared graph-write transaction. The behaviour was previously exercised only by
the integration e2e suite (``tests/integration/cli/test_apply_pipeline_e2e.py``),
which the changed-lines coverage gate does NOT run, so every branch of the
translation was uncovered on the PR.

These tests run the REAL CRUD functions against a real (in-memory SQLite)
session and read the ``policy_gates`` rows back — the row lifecycle, not an
event-shaped mock, is what is asserted. ``append_audit_event`` emits are
best-effort and fail-open, so they are patched here (a real audit chain is not
the subject of this module); the fail-open branch is exercised explicitly.

The Postgres-only advisory-lock/timeout statements are bypassed at the session
boundary so the surrounding Python (the lock helper, the create/update/delete
control flow, the ``RESET`` cleanup) still executes on SQLite.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.db.crud import pipeline as pipeline_crud
from modulo.db.crud.pipeline import (
    _declared_policy_gates,
    _ensure_declared_gate,
    _iter_declared_policy_gates,
    _load_gate_eval_for_node,
    _reject_duplicate_policy_gates,
    _soft_delete_declared_gate,
    reconcile_graph_policy_gates,
)
from modulo.db.models.account import Account
from modulo.db.models.base import Base
from modulo.db.models.eval import Eval
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.policy_gate import PolicyGate
from modulo.db.soft_delete import include_soft_deleted

# Scoped create_all: unrelated models use Postgres-only column types that SQLite
# cannot render (same rationale as tests/unit/db/test_seed_demo.py).
_TABLE_NAMES = {"accounts", "pipelines", "evals", "policy_gates"}

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-00000000000a")
_ACCOUNT_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
_PIPELINE_ID = uuid.UUID("00000000-0000-0000-0000-0000000000f1")
_EVAL_ID = uuid.UUID("660e8400-e29b-41d4-a716-446655440001")
_OTHER_EVAL_ID = uuid.UUID("660e8400-e29b-41d4-a716-446655440002")

_NODE_ID = "11111111-1111-1111-1111-1111a1109101"
_OTHER_NODE_ID = "22222222-2222-2222-2222-2222a1109102"

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


def _gate_node(
    node_id: str = _NODE_ID,
    eval_id: str = str(_EVAL_ID),
    action: str = "block",
) -> dict[str, Any]:
    return {"id": node_id, "policy_gate": {"eval_id": eval_id, "action": action}}


async def _seed_eval(
    session: AsyncSession,
    *,
    eval_id: uuid.UUID = _EVAL_ID,
    node_id: str | None = _NODE_ID,
    pipeline_id: uuid.UUID = _PIPELINE_ID,
) -> Eval:
    session.add(Account(id=_ACCOUNT_ID, email="far1109-gate@example.com", display_name="FAR-1109 Gate"))
    session.add(Pipeline(id=_PIPELINE_ID, organisation_id=_ORG_ID, account_id=_ACCOUNT_ID, name="gate-pipeline"))
    eval_row = Eval(
        id=eval_id,
        organisation_id=_ORG_ID,
        pipeline_id=pipeline_id,
        node_id=uuid.UUID(node_id) if node_id is not None else None,
        account_id=_ACCOUNT_ID,
        eval_type="llm_judge",
        config_json={},
        version=1,
    )
    session.add(eval_row)
    await session.flush()
    return eval_row


async def _live_gates(session: AsyncSession) -> list[PolicyGate]:
    result = await session.execute(select(PolicyGate))
    return list(result.scalars())


class TestIterDeclaredPolicyGates:
    def test_extracts_only_well_formed_gate_blocks(self) -> None:
        nodes: list[Any] = [
            "not-a-dict",
            {"id": "n1"},
            {"id": "n2", "policy_gate": "block"},
            {"policy_gate": {"eval_id": str(_EVAL_ID), "action": "block"}},
            {"id": "n4", "policy_gate": {"action": "block"}},
            _gate_node("n5"),
        ]
        assert _iter_declared_policy_gates(nodes) == [(str(_EVAL_ID), "n5", "block")]

    def test_declared_map_keys_by_eval_id(self) -> None:
        assert _declared_policy_gates([_gate_node("n5", action="warn")]) == {
            str(_EVAL_ID): {"node_id": "n5", "action": "warn"}
        }


class TestRejectDuplicatePolicyGates:
    def test_duplicate_eval_on_two_nodes_is_rejected(self) -> None:
        with pytest.raises(HTTPException) as exc:
            _reject_duplicate_policy_gates([_gate_node("n1"), _gate_node("n2")])
        assert exc.value.status_code == 422

    def test_distinct_evals_are_allowed(self) -> None:
        nodes = [_gate_node("n1"), _gate_node("n2", eval_id=str(_OTHER_EVAL_ID))]
        assert _reject_duplicate_policy_gates(nodes) is None


class TestLoadGateEvalForNode:
    async def test_returns_the_bound_eval(self, session: AsyncSession) -> None:
        await _seed_eval(session)
        row = await _load_gate_eval_for_node(
            session, eval_id=_EVAL_ID, org_id=_ORG_ID, pipeline_id=_PIPELINE_ID, node_id=_NODE_ID
        )
        assert row.id == _EVAL_ID

    async def test_missing_eval_is_rejected(self, session: AsyncSession) -> None:
        with pytest.raises(HTTPException) as exc:
            await _load_gate_eval_for_node(
                session, eval_id=_OTHER_EVAL_ID, org_id=_ORG_ID, pipeline_id=_PIPELINE_ID, node_id=_NODE_ID
            )
        assert exc.value.status_code == 422

    async def test_cross_pipeline_eval_is_rejected(self, session: AsyncSession) -> None:
        await _seed_eval(session)
        with pytest.raises(HTTPException) as exc:
            await _load_gate_eval_for_node(
                session, eval_id=_EVAL_ID, org_id=_ORG_ID, pipeline_id=uuid.uuid4(), node_id=_NODE_ID
            )
        assert exc.value.status_code == 422

    async def test_unbound_node_eval_is_rejected(self, session: AsyncSession) -> None:
        await _seed_eval(session, node_id=None)
        with pytest.raises(HTTPException) as exc:
            await _load_gate_eval_for_node(
                session, eval_id=_EVAL_ID, org_id=_ORG_ID, pipeline_id=_PIPELINE_ID, node_id=_NODE_ID
            )
        assert exc.value.status_code == 422

    async def test_mismatched_node_eval_is_rejected(self, session: AsyncSession) -> None:
        await _seed_eval(session)
        with pytest.raises(HTTPException) as exc:
            await _load_gate_eval_for_node(
                session, eval_id=_EVAL_ID, org_id=_ORG_ID, pipeline_id=_PIPELINE_ID, node_id=_OTHER_NODE_ID
            )
        assert exc.value.status_code == 422


class TestAuditGateTranslation:
    async def test_success_emits_the_event(self, session: AsyncSession) -> None:
        gate = PolicyGate(
            organisation_id=_ORG_ID,
            eval_id=_EVAL_ID,
            node_id=uuid.UUID(_NODE_ID),
            action="block",
            version=1,
            enabled=True,
            enabled_at=datetime.now(UTC),
        )
        audit = AsyncMock()
        with patch.object(pipeline_crud, "append_audit_event", new=audit):
            await pipeline_crud._audit_gate_translation(
                session,
                org_id=_ORG_ID,
                account_id=_ACCOUNT_ID,
                event_type="policy_gate.created",
                gate=gate,
                payload_json={"eval_id": str(_EVAL_ID)},
            )
        audit.assert_awaited_once()

    async def test_audit_failure_is_swallowed(self, session: AsyncSession) -> None:
        gate = PolicyGate(
            organisation_id=_ORG_ID,
            eval_id=_EVAL_ID,
            node_id=uuid.UUID(_NODE_ID),
            action="block",
            version=1,
            enabled=True,
            enabled_at=datetime.now(UTC),
        )
        audit = AsyncMock(side_effect=RuntimeError("audit backend down"))
        with patch.object(pipeline_crud, "append_audit_event", new=audit):
            await pipeline_crud._audit_gate_translation(
                session,
                org_id=_ORG_ID,
                account_id=_ACCOUNT_ID,
                event_type="policy_gate.created",
                gate=gate,
                payload_json={"eval_id": str(_EVAL_ID)},
            )
        audit.assert_awaited_once()


class TestEnsureDeclaredGate:
    async def test_invalid_action_is_rejected(self, session: AsyncSession) -> None:
        eval_row = await _seed_eval(session)
        with pytest.raises(HTTPException) as exc:
            await _ensure_declared_gate(
                session, org_id=_ORG_ID, account_id=_ACCOUNT_ID, eval_row=eval_row, action="explode"
            )
        assert exc.value.status_code == 422

    async def test_creates_then_updates_the_gate_in_place(self, session: AsyncSession) -> None:
        eval_row = await _seed_eval(session)
        audit = AsyncMock()
        with patch.object(pipeline_crud, "append_audit_event", new=audit):
            await _ensure_declared_gate(
                session, org_id=_ORG_ID, account_id=_ACCOUNT_ID, eval_row=eval_row, action="block"
            )
            await session.flush()
            gates = await _live_gates(session)
            assert len(gates) == 1
            assert gates[0].action == "block"
            assert gates[0].version == 1
            assert gates[0].enabled is True
            assert gates[0].enabled_at is not None

            # A repeated declaration with the same action is a no-op (no churn).
            await _ensure_declared_gate(
                session, org_id=_ORG_ID, account_id=_ACCOUNT_ID, eval_row=eval_row, action="block"
            )
            await session.flush()
            gates = await _live_gates(session)
            assert len(gates) == 1
            assert gates[0].version == 1

            # A changed action updates the SAME row in place, bumping the version.
            await _ensure_declared_gate(
                session, org_id=_ORG_ID, account_id=_ACCOUNT_ID, eval_row=eval_row, action="warn"
            )
            await session.flush()
            gates = await _live_gates(session)
            assert len(gates) == 1
            assert gates[0].action == "warn"
            assert gates[0].version == 2
            assert gates[0].pre_version_raw == {"action": "block"}

        assert [call.kwargs["event_type"] for call in audit.await_args_list] == [
            "policy_gate.created",
            "policy_gate.updated",
        ]


class TestSoftDeleteDeclaredGate:
    async def test_noop_when_no_live_gate(self, session: AsyncSession) -> None:
        audit = AsyncMock()
        with patch.object(pipeline_crud, "append_audit_event", new=audit):
            await _soft_delete_declared_gate(session, org_id=_ORG_ID, account_id=_ACCOUNT_ID, eval_id=_EVAL_ID)
        audit.assert_not_awaited()

    async def test_soft_deletes_the_live_gate(self, session: AsyncSession) -> None:
        await _seed_eval(session)
        session.add(
            PolicyGate(
                organisation_id=_ORG_ID,
                eval_id=_EVAL_ID,
                node_id=uuid.UUID(_NODE_ID),
                action="block",
                version=1,
                enabled=True,
                enabled_at=datetime.now(UTC),
            )
        )
        await session.flush()

        audit = AsyncMock()
        with patch.object(pipeline_crud, "append_audit_event", new=audit):
            await _soft_delete_declared_gate(session, org_id=_ORG_ID, account_id=_ACCOUNT_ID, eval_id=_EVAL_ID)
            await session.flush()

        rows = list((await session.execute(include_soft_deleted(select(PolicyGate)))).scalars())
        assert len(rows) == 1
        assert rows[0].deleted_at is not None
        assert rows[0].deleted_by == _ACCOUNT_ID
        assert audit.await_args.kwargs["event_type"] == "policy_gate.deleted"

        # A soft-deleted gate is no longer live.
        assert not await _live_gates(session)


class TestReconcileGraphPolicyGates:
    async def test_no_declared_gates_is_a_noop(self, session: AsyncSession) -> None:
        audit = AsyncMock()
        with patch.object(pipeline_crud, "append_audit_event", new=audit):
            await reconcile_graph_policy_gates(
                session,
                org_id=_ORG_ID,
                account_id=_ACCOUNT_ID,
                pipeline_id=_PIPELINE_ID,
                previous_nodes=[{"id": _NODE_ID}],
                new_nodes=[{"id": _NODE_ID}],
            )
        audit.assert_not_awaited()

    async def test_invalid_eval_uuid_is_rejected(self, session: AsyncSession) -> None:
        with pytest.raises(HTTPException) as exc:
            await reconcile_graph_policy_gates(
                session,
                org_id=_ORG_ID,
                account_id=_ACCOUNT_ID,
                pipeline_id=_PIPELINE_ID,
                previous_nodes=[],
                new_nodes=[_gate_node(eval_id="not-a-uuid")],
            )
        assert exc.value.status_code == 422

    async def test_missing_eval_blocks_the_write(self, session: AsyncSession) -> None:
        with pytest.raises(HTTPException) as exc:
            await reconcile_graph_policy_gates(
                session,
                org_id=_ORG_ID,
                account_id=_ACCOUNT_ID,
                pipeline_id=_PIPELINE_ID,
                previous_nodes=[],
                new_nodes=[_gate_node()],
            )
        assert exc.value.status_code == 422

    async def test_create_then_remove_lifecycle(self, session: AsyncSession) -> None:
        await _seed_eval(session)
        audit = AsyncMock()
        with patch.object(pipeline_crud, "append_audit_event", new=audit):
            await reconcile_graph_policy_gates(
                session,
                org_id=_ORG_ID,
                account_id=_ACCOUNT_ID,
                pipeline_id=_PIPELINE_ID,
                previous_nodes=[],
                new_nodes=[_gate_node()],
            )
            await session.flush()
            live = await _live_gates(session)
            assert len(live) == 1
            assert live[0].action == "block"

            await reconcile_graph_policy_gates(
                session,
                org_id=_ORG_ID,
                account_id=_ACCOUNT_ID,
                pipeline_id=_PIPELINE_ID,
                previous_nodes=[_gate_node()],
                new_nodes=[],
            )
            await session.flush()

        assert not await _live_gates(session)
        assert [call.kwargs["event_type"] for call in audit.await_args_list] == [
            "policy_gate.created",
            "policy_gate.deleted",
        ]
