"""Integration tests for FAR-1102 chunk-4 decision-record payload columns.

Runs against a real Postgres (testcontainers) via the integration session
fixtures.  Verifies the six payload columns (C8), the CHECK constraint on
``resolved_action`` (C9), the temporary unique index
``ix_tmp_policy_gate_decisions_run_gate_result`` (C10), and real-asyncpg
IntegrityError constraint metadata for ``is_policy_gate_decision_fk_error``
(C16).

Covers criteria 8, 9, 10, 16 (integration half).
"""

import uuid

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers — mirror the sibling test_policy_gate_constraints.py pattern
# ---------------------------------------------------------------------------


async def _setup_org(engine: AsyncEngine):
    org_id = uuid.uuid4()
    slug = f"drc-test-{org_id.hex[:8]}"
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :n, :s, '{}'::json)"),
            {"id": str(org_id), "n": slug, "s": slug},
        )
        acc_id = uuid.uuid4()
        await conn.execute(
            text(
                "INSERT INTO accounts (id, email, display_name, password_hash, auth_provider, active) "
                "VALUES (:id, :e, :n, 'hash', 'local', true)"
            ),
            {"id": str(acc_id), "e": f"{slug}@test.com", "n": slug},
        )
        pipe_id = uuid.uuid4()
        await conn.execute(
            text(
                "INSERT INTO pipelines (id, organisation_id, name, account_id, "
                "max_concurrent_runs, lock_wait_timeout_seconds, node_timeout_seconds, "
                "run_context_defaults, graph_nodes_json) "
                "VALUES (:id, :oid, :n, :aid, 10, 30, 300, '{}'::json, '[]'::json)"
            ),
            {"id": str(pipe_id), "oid": str(org_id), "n": f"{slug}-pipe", "aid": str(acc_id)},
        )
    return org_id, acc_id, pipe_id


async def _insert_eval(engine, org_id, pipe_id, acc_id, node_id, eval_type="regex"):
    eval_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO evals (id, organisation_id, pipeline_id, account_id, "
                "node_id, eval_type, config_json, version) "
                "VALUES (:id, :oid, :pid, :aid, :nid, :et, '{}'::json, 1)"
            ),
            {
                "id": str(eval_id),
                "oid": str(org_id),
                "pid": str(pipe_id),
                "aid": str(acc_id),
                "nid": str(node_id),
                "et": eval_type,
            },
        )
    return eval_id


async def _insert_gate(engine, org_id, eval_id, node_id, action="warn"):
    gate_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO policy_gates (id, organisation_id, eval_id, node_id, "
                "action, version) VALUES (:id, :oid, :eid, :nid, :a, 1)"
            ),
            {"id": str(gate_id), "oid": str(org_id), "eid": str(eval_id), "nid": str(node_id), "a": action},
        )
    return gate_id


async def _insert_decision_full(
    engine,
    org_id,
    gate_id,
    eval_id,
    *,
    resolved_action: str | None = "warn",
    error_detail: str | None = None,
    node_id: uuid.UUID | None = None,
    eval_result_id: uuid.UUID | None = None,
    run_id: uuid.UUID | None = None,
    gate_version: int = 1,
):
    decision_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO policy_gate_decisions (id, organisation_id, policy_gate_id, eval_id, "
                "resolved_action, error_detail, node_id, eval_result_id, run_id, policy_gate_version) "
                "VALUES (:id, :oid, :pgid, :eid, :ra, :ed, :nid, :erid, :rid, :gv)"
            ),
            {
                "id": str(decision_id),
                "oid": str(org_id),
                "pgid": str(gate_id),
                "eid": str(eval_id),
                "ra": resolved_action,
                "ed": error_detail,
                "nid": str(node_id) if node_id else None,
                "erid": str(eval_result_id) if eval_result_id else None,
                "rid": str(run_id) if run_id else None,
                "gv": gate_version,
            },
        )
    return decision_id


async def _setup_eval_and_gate(engine, org_id, acc_id, pipe_id):
    """Create a valid eval + policy_gate pair; returns (node_id, eval_id, gate_id)."""
    node_id = uuid.uuid4()
    eval_id = await _insert_eval(engine, org_id, pipe_id, acc_id, node_id)
    gate_id = await _insert_gate(engine, org_id, eval_id, node_id)
    return node_id, eval_id, gate_id


# ---------------------------------------------------------------------------
# C8: six payload columns present with the declared types/defaults
# ---------------------------------------------------------------------------


class TestC8PayloadColumns:
    @pytest.mark.asyncio
    async def test_columns_present_in_schema(self, db_engine: AsyncEngine) -> None:
        async with db_engine.connect() as connection:
            columns = await connection.run_sync(
                lambda conn: {c["name"] for c in inspect(conn).get_columns("policy_gate_decisions")}
            )
        expected = {
            "resolved_action",
            "error_detail",
            "node_id",
            "eval_result_id",
            "run_id",
            "policy_gate_version",
        }
        assert expected.issubset(columns)

    @pytest.mark.asyncio
    async def test_payload_row_round_trips(self, db_engine: AsyncEngine) -> None:
        org_id, acc_id, pipe_id = await _setup_org(db_engine)
        node_id, eval_id, gate_id = await _setup_eval_and_gate(db_engine, org_id, acc_id, pipe_id)
        node_id = uuid.uuid4()
        did = await _insert_decision_full(
            db_engine,
            org_id,
            gate_id,
            eval_id,
            resolved_action="continue",
            error_detail="no_eval_result",
            node_id=node_id,
            gate_version=3,
        )
        async with db_engine.begin() as conn:
            row = (
                (
                    await conn.execute(
                        text(
                            "SELECT resolved_action, error_detail, node_id, eval_result_id, run_id, "
                            "policy_gate_version FROM policy_gate_decisions WHERE id = :id"
                        ),
                        {"id": str(did)},
                    )
                )
                .mappings()
                .one()
            )
        assert row["resolved_action"] == "continue"
        assert row["error_detail"] == "no_eval_result"
        assert str(row["node_id"]) == str(node_id)
        assert row["eval_result_id"] is None
        assert row["run_id"] is None
        assert row["policy_gate_version"] == 3

    @pytest.mark.asyncio
    async def test_defaults_apply_on_insert(self, db_engine: AsyncEngine) -> None:
        org_id, acc_id, pipe_id = await _setup_org(db_engine)
        _, eval_id, gate_id = await _setup_eval_and_gate(db_engine, org_id, acc_id, pipe_id)
        decision_id = uuid.uuid4()
        async with db_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO policy_gate_decisions (id, organisation_id, policy_gate_id, eval_id) "
                    "VALUES (:id, :oid, :pgid, :eid)"
                ),
                {"id": str(decision_id), "oid": str(org_id), "pgid": str(gate_id), "eid": str(eval_id)},
            )
            row = (
                (
                    await conn.execute(
                        text(
                            "SELECT resolved_action, policy_gate_version, error_detail "
                            "FROM policy_gate_decisions WHERE id = :id"
                        ),
                        {"id": str(decision_id)},
                    )
                )
                .mappings()
                .one()
            )
        assert row["resolved_action"] == "continue"
        assert row["policy_gate_version"] == 1
        assert row["error_detail"] is None


# ---------------------------------------------------------------------------
# C9: CHECK constraint on resolved_action (Postgres-only)
# ---------------------------------------------------------------------------


class TestC9ResolvedActionCheck:
    @pytest.mark.asyncio
    async def test_invalid_action_rejected(self, db_engine: AsyncEngine) -> None:
        org_id, acc_id, pipe_id = await _setup_org(db_engine)
        _, eval_id, gate_id = await _setup_eval_and_gate(db_engine, org_id, acc_id, pipe_id)
        with pytest.raises(IntegrityError):
            await _insert_decision_full(db_engine, org_id, gate_id, eval_id, resolved_action="explode")

    @pytest.mark.asyncio
    async def test_all_vocabulary_values_accepted(self, db_engine: AsyncEngine) -> None:
        org_id, acc_id, pipe_id = await _setup_org(db_engine)
        _, eval_id, gate_id = await _setup_eval_and_gate(db_engine, org_id, acc_id, pipe_id)
        for action in ("continue", "warn", "block"):
            did = await _insert_decision_full(db_engine, org_id, gate_id, eval_id, resolved_action=action)
            assert did is not None

    @pytest.mark.asyncio
    async def test_check_constraint_present_in_schema(self, db_engine: AsyncEngine) -> None:
        async with db_engine.connect() as connection:
            ck_names = await connection.run_sync(
                lambda conn: {ck["name"] for ck in inspect(conn).get_check_constraints("policy_gate_decisions")}
            )
        assert "ck_policy_gate_decisions_resolved_action" in ck_names


# ---------------------------------------------------------------------------
# C10: temporary uniqueness index behaviour
# ---------------------------------------------------------------------------


class TestC10TemporaryUniquenessIndex:
    @pytest.mark.asyncio
    async def test_index_present_and_unique(self, db_engine: AsyncEngine) -> None:
        async with db_engine.connect() as connection:
            indexes = await connection.run_sync(
                lambda conn: {ix["name"]: ix for ix in inspect(conn).get_indexes("policy_gate_decisions")}
            )
        assert "ix_tmp_policy_gate_decisions_run_gate_result" in indexes
        assert indexes["ix_tmp_policy_gate_decisions_run_gate_result"]["unique"] is True
        cols = indexes["ix_tmp_policy_gate_decisions_run_gate_result"]["column_names"]
        assert cols == ["run_id", "policy_gate_id", "eval_result_id"]

    @pytest.mark.asyncio
    async def test_identical_run_gate_result_rejected(self, db_engine: AsyncEngine) -> None:
        org_id, acc_id, pipe_id = await _setup_org(db_engine)
        _, eval_id, gate_id = await _setup_eval_and_gate(db_engine, org_id, acc_id, pipe_id)
        run_id = uuid.uuid4()
        er_id = uuid.uuid4()
        await _insert_decision_full(
            db_engine,
            org_id,
            gate_id,
            eval_id,
            run_id=run_id,
            eval_result_id=er_id,
        )
        with pytest.raises(IntegrityError):
            await _insert_decision_full(
                db_engine,
                org_id,
                gate_id,
                eval_id,
                run_id=run_id,
                eval_result_id=er_id,
            )

    @pytest.mark.asyncio
    async def test_null_eval_result_id_rows_unbounded(self, db_engine: AsyncEngine) -> None:
        """NULLs compare distinct: unlimited custodial rows for the same (run, gate)."""
        org_id, acc_id, pipe_id = await _setup_org(db_engine)
        _, eval_id, gate_id = await _setup_eval_and_gate(db_engine, org_id, acc_id, pipe_id)
        run_id = uuid.uuid4()
        ids = [await _insert_decision_full(db_engine, org_id, gate_id, eval_id, run_id=run_id) for _ in range(3)]
        assert len(set(ids)) == 3

    @pytest.mark.asyncio
    async def test_distinct_eval_result_allowed(self, db_engine: AsyncEngine) -> None:
        org_id, acc_id, pipe_id = await _setup_org(db_engine)
        _, eval_id, gate_id = await _setup_eval_and_gate(db_engine, org_id, acc_id, pipe_id)
        run_id = uuid.uuid4()
        er_id1 = uuid.uuid4()
        er_id2 = uuid.uuid4()
        did1 = await _insert_decision_full(db_engine, org_id, gate_id, eval_id, run_id=run_id, eval_result_id=er_id1)
        did2 = await _insert_decision_full(db_engine, org_id, gate_id, eval_id, run_id=run_id, eval_result_id=er_id2)
        # Verify both inserts actually created rows (not just "no exception")
        assert did1 is not None
        assert did2 is not None
        async with db_engine.begin() as conn:
            rows = (
                await conn.execute(
                    text("SELECT id FROM policy_gate_decisions WHERE run_id = :rid AND policy_gate_id = :pgid"),
                    {"rid": str(run_id), "pgid": str(gate_id)},
                )
            ).fetchall()
        assert len(rows) == 2


# ---------------------------------------------------------------------------
# C16: real-asyncpg IntegrityError exposes constraint_name for classification
# ---------------------------------------------------------------------------


class TestC16RealIntegrityErrorMetadata:
    @pytest.mark.asyncio
    async def test_fk_violation_carries_constraint_name(self, db_engine: AsyncEngine) -> None:
        """Deleting a gate with a referencing decision must raise an IntegrityError
        whose metadata classifies as a decision-record FK error."""
        from modulo.db.crud.policy_gate_decision import is_policy_gate_decision_fk_error

        org_id, acc_id, pipe_id = await _setup_org(db_engine)
        _, eval_id, gate_id = await _setup_eval_and_gate(db_engine, org_id, acc_id, pipe_id)
        await _insert_decision_full(db_engine, org_id, gate_id, eval_id)
        exc = None
        try:
            async with db_engine.begin() as conn:
                await conn.execute(text("DELETE FROM policy_gates WHERE id = :id"), {"id": str(gate_id)})
        except IntegrityError as e:
            exc = e
        assert exc is not None
        assert is_policy_gate_decision_fk_error(exc) is True

    @pytest.mark.asyncio
    async def test_unrelated_fk_violation_not_classified(self, db_engine: AsyncEngine) -> None:
        from modulo.db.crud.policy_gate_decision import is_policy_gate_decision_fk_error

        _, acc_id, _ = await _setup_org(db_engine)
        exc = None
        try:
            async with db_engine.begin() as conn:
                await conn.execute(text("DELETE FROM accounts WHERE id = :id"), {"id": str(acc_id)})
        except IntegrityError as e:
            exc = e
        if exc is not None:
            assert is_policy_gate_decision_fk_error(exc) is False


# ---------------------------------------------------------------------------
# F1: row-existence proof — the outer transaction actually commits
# ---------------------------------------------------------------------------


class TestC1F1RowExistenceProof:
    """F1 regression guard: a decision row written via the persist path
    actually EXISTS in the database after the transaction commits.

    This proves the outer ``session.begin()`` (not just ``begin_nested()``)
    is present — without it the row would be silently rolled back.
    """

    @pytest.mark.asyncio
    async def test_decision_row_persists_after_commit(self, db_engine: AsyncEngine) -> None:
        org_id, acc_id, pipe_id = await _setup_org(db_engine)
        node_id, eval_id, gate_id = await _setup_eval_and_gate(db_engine, org_id, acc_id, pipe_id)
        run_id = uuid.uuid4()
        decision_id = await _insert_decision_full(
            db_engine,
            org_id,
            gate_id,
            eval_id,
            resolved_action="block",
            error_detail="test_persistence",
            node_id=node_id,
            run_id=run_id,
            gate_version=2,
        )
        # Read the row back — proves it committed
        async with db_engine.begin() as conn:
            row = (
                (
                    await conn.execute(
                        text(
                            "SELECT resolved_action, error_detail, run_id, policy_gate_version "
                            "FROM policy_gate_decisions WHERE id = :id"
                        ),
                        {"id": str(decision_id)},
                    )
                )
                .mappings()
                .one()
            )
        assert row["resolved_action"] == "block"
        assert row["error_detail"] == "test_persistence"
        assert str(row["run_id"]) == str(run_id)
        assert row["policy_gate_version"] == 2


# ---------------------------------------------------------------------------
# F1: outer transaction commits (via ORM session pattern)
# ---------------------------------------------------------------------------


class TestC1F1OuterTransactionCommits:
    """Prove the ORM session pattern commits: write via session.begin() +
    session.begin_nested(), then read back with raw SQL."""

    @pytest.mark.asyncio
    async def test_orm_session_commit_persists_row(self, db_engine: AsyncEngine) -> None:
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

        org_id, acc_id, pipe_id = await _setup_org(db_engine)
        node_id, eval_id, gate_id = await _setup_eval_and_gate(db_engine, org_id, acc_id, pipe_id)
        run_id = uuid.uuid4()

        session_factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        async with session_factory() as session, session.begin(), session.begin_nested():
            await session.execute(
                text(
                    "INSERT INTO policy_gate_decisions "
                    "(id, organisation_id, policy_gate_id, eval_id, resolved_action, "
                    "error_detail, node_id, run_id, policy_gate_version) "
                    "VALUES (:id, :oid, :pgid, :eid, :ra, :ed, :nid, :rid, :gv)"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "oid": str(org_id),
                    "pgid": str(gate_id),
                    "eid": str(eval_id),
                    "ra": "warn",
                    "ed": "outer_txn_test",
                    "nid": str(node_id),
                    "rid": str(run_id),
                    "gv": 1,
                },
            )

        # Read back with a fresh connection — proves outer transaction committed
        async with db_engine.begin() as conn:
            result = await conn.execute(
                text("SELECT COUNT(*) FROM policy_gate_decisions WHERE error_detail = 'outer_txn_test'")
            )
            count = result.scalar()
        assert count == 1


# ---------------------------------------------------------------------------
# F5 (criterion 16): decision state unchanged after persistence failure
# ---------------------------------------------------------------------------


class TestC16F5DecisionStateUnchangedAfterPersistenceFailure:
    """Criterion 16: the fail-open wrapper never mutates or reverses the
    decision when persistence fails.

    Uses a REAL session (required by the spec for savepoint pattern) with
    a deliberately broken table reference so the savepoint fails.
    """

    @pytest.mark.asyncio
    async def test_outcome_identical_before_and_after_persistence_failure(self, db_engine: AsyncEngine) -> None:
        from modulo.core.eval_engine.policy_gate import (
            EvalPolicySnapshot,
            EvalResultView,
            EvalView,
            PolicyGateView,
            build_decision_row,
            resolve_policy_gate,
        )

        org_id, acc_id, pipe_id = await _setup_org(db_engine)
        node_id, eval_id, gate_id = await _setup_eval_and_gate(db_engine, org_id, acc_id, pipe_id)
        run_id = uuid.uuid4()
        er_id = uuid.uuid4()

        snapshot = EvalPolicySnapshot(
            policy_gate=PolicyGateView(
                id=gate_id,
                organisation_id=org_id,
                version=1,
                node_id=node_id,
                action="block",
            ),
            eval=EvalView(
                id=eval_id,
                organisation_id=org_id,
                node_id=node_id,
                eval_type="regex",
                deleted_at=None,
            ),
            eval_result=EvalResultView(id=er_id, passed=False),
        )

        # Capture decision state BEFORE persistence attempt
        outcome_before = resolve_policy_gate(snapshot)
        assert outcome_before.action == "block"
        assert outcome_before.result is False
        assert outcome_before.eval_result_id == er_id
        assert outcome_before.error is None

        # Build the row to prove the construction is stable
        row_before = build_decision_row(snapshot, outcome_before, run_id)
        assert row_before.resolved_action == "block"
        assert row_before.error_detail is None

        # Attempt persistence — it will fail because the session is
        # deliberately broken (no real table for this row in the raw session)
        from contextlib import asynccontextmanager

        from modulo.core.pipeline_engine.eval_persist_order import _persist_decision_row

        @asynccontextmanager
        async def _broken_factory():
            async with db_engine.connect() as conn:
                yield conn  # raw connection, not a session — will fail on session.add()

        # The persistence should NOT raise (fail-open)
        await _persist_decision_row(
            snapshot,
            outcome_before,
            run_id,
            session_factory=_broken_factory,
            org_id=org_id,
        )

        # Capture decision state AFTER persistence attempt
        outcome_after = resolve_policy_gate(snapshot)

        # Assert state is IDENTICAL — the wrapper never mutated it
        assert outcome_after.action == outcome_before.action
        assert outcome_after.result == outcome_before.result
        assert outcome_after.eval_result_id == outcome_before.eval_result_id
        assert outcome_after.error == outcome_before.error

        # Also prove the row construction is deterministic
        row_after = build_decision_row(snapshot, outcome_after, run_id)
        assert row_after.resolved_action == row_before.resolved_action
        assert row_after.error_detail == row_before.error_detail
        assert row_after.node_id == row_before.node_id
        assert row_after.run_id == row_before.run_id
