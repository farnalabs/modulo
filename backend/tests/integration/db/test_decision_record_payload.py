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
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.integration


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


# ---------------------------------------------------------------------------
# C8: six payload columns present with the declared types/defaults
# ---------------------------------------------------------------------------


class TestC8PayloadColumns:
    @pytest.mark.asyncio
    async def test_columns_present_in_schema(self, db_engine: AsyncEngine) -> None:
        from sqlalchemy import inspect

        inspector = inspect(db_engine.sync_engine)
        columns = {c["name"] for c in inspector.get_columns("policy_gate_decisions")}
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
        gate_id, eval_id = uuid.uuid4(), uuid.uuid4()
        async with db_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO evals (id, organisation_id, pipeline_id, account_id, "
                    "node_id, eval_type, config_json, version) "
                    "VALUES (:id, :oid, :pid, :aid, :nid, 'regex', '{}'::json, 1)"
                ),
                {
                    "id": str(eval_id),
                    "oid": str(org_id),
                    "pid": str(pipe_id),
                    "aid": str(acc_id),
                    "nid": str(uuid.uuid4()),
                },
            )
            await conn.execute(
                text(
                    "INSERT INTO policy_gates (id, organisation_id, eval_id, node_id, "
                    "action, version) VALUES (:id, :oid, :eid, :nid, 'warn', 3)"
                ),
                {"id": str(gate_id), "oid": str(org_id), "eid": str(eval_id), "nid": str(uuid.uuid4())},
            )
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
        org_id, _, _ = await _setup_org(db_engine)
        payload = {"id": str(uuid.uuid4()), "oid": str(org_id), "pgid": str(uuid.uuid4()), "eid": str(uuid.uuid4())}
        async with db_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO policy_gate_decisions (id, organisation_id, policy_gate_id, eval_id) "
                    "VALUES (:id, :oid, :pgid, :eid)"
                ),
                payload,
            )
            row = (
                (
                    await conn.execute(
                        text(
                            "SELECT resolved_action, policy_gate_version, error_detail "
                            "FROM policy_gate_decisions WHERE id = :id"
                        ),
                        {"id": payload["id"]},
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
        org_id, _, _ = await _setup_org(db_engine)
        with pytest.raises(IntegrityError):
            await _insert_decision_full(db_engine, org_id, uuid.uuid4(), uuid.uuid4(), resolved_action="explode")

    @pytest.mark.asyncio
    async def test_all_vocabulary_values_accepted(self, db_engine: AsyncEngine) -> None:
        org_id, _, _ = await _setup_org(db_engine)
        for action in ("continue", "warn", "block"):
            did = await _insert_decision_full(db_engine, org_id, uuid.uuid4(), uuid.uuid4(), resolved_action=action)
            assert did is not None

    @pytest.mark.asyncio
    async def test_check_constraint_present_in_schema(self, db_engine: AsyncEngine) -> None:
        from sqlalchemy import inspect

        inspector = inspect(db_engine.sync_engine)
        ck_names = {ck["name"] for ck in inspector.get_check_constraints("policy_gate_decisions")}
        assert "ck_policy_gate_decisions_resolved_action" in ck_names


# ---------------------------------------------------------------------------
# C10: temporary uniqueness index behaviour
# ---------------------------------------------------------------------------


class TestC10TemporaryUniquenessIndex:
    @pytest.mark.asyncio
    async def test_index_present_and_unique(self, db_engine: AsyncEngine) -> None:
        from sqlalchemy import inspect

        inspector = inspect(db_engine.sync_engine)
        indexes = {ix["name"]: ix for ix in inspector.get_indexes("policy_gate_decisions")}
        assert "ix_tmp_policy_gate_decisions_run_gate_result" in indexes
        assert indexes["ix_tmp_policy_gate_decisions_run_gate_result"]["unique"] is True
        cols = indexes["ix_tmp_policy_gate_decisions_run_gate_result"]["column_names"]
        assert cols == ["run_id", "policy_gate_id", "eval_result_id"]

    @pytest.mark.asyncio
    async def test_identical_run_gate_result_rejected(self, db_engine: AsyncEngine) -> None:
        org_id, _, _ = await _setup_org(db_engine)
        gate_id, eval_id, run_id, er_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
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
        org_id, _, _ = await _setup_org(db_engine)
        gate_id, eval_id, run_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        ids = [await _insert_decision_full(db_engine, org_id, gate_id, eval_id, run_id=run_id) for _ in range(3)]
        assert len(set(ids)) == 3

    @pytest.mark.asyncio
    async def test_distinct_eval_result_allowed(self, db_engine: AsyncEngine) -> None:
        org_id, _, _ = await _setup_org(db_engine)
        gate_id, eval_id, run_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        await _insert_decision_full(db_engine, org_id, gate_id, eval_id, run_id=run_id, eval_result_id=uuid.uuid4())
        await _insert_decision_full(db_engine, org_id, gate_id, eval_id, run_id=run_id, eval_result_id=uuid.uuid4())


# ---------------------------------------------------------------------------
# C16: real-asyncpg IntegrityError exposes constraint_name for classification
# ---------------------------------------------------------------------------


class TestC16RealIntegrityErrorMetadata:
    @pytest.mark.asyncio
    async def test_fk_violation_carries_constraint_name(self, db_engine: AsyncEngine) -> None:
        """Deleting a gate with a referencing decision must raise an IntegrityError
        whose metadata classifies as a decision-record FK error."""
        from modulo.db.crud.policy_gate_decision import is_policy_gate_decision_fk_error

        org_id, _, _ = await _setup_org(db_engine)
        gate_id, eval_id = uuid.uuid4(), uuid.uuid4()
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
