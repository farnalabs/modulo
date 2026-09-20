"""Integration tests for PolicyGate schema constraints (FAR-1060, chunk 1).

Runs against a real Postgres (testcontainers) via the integration session
fixtures.  Verifies partial unique indexes, composite FK enforcement, FK
delete actions, and schema-reflection contracts that a unit test cannot catch.

Covers criteria 4, 12, 13, 15a (integration half), 16.
"""

import uuid

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _setup_org(engine: AsyncEngine):
    org_id = uuid.uuid4()
    slug = f"pg-test-{org_id.hex[:8]}"
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


async def _insert_decision(engine, org_id, gate_id, eval_id):
    decision_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO policy_gate_decisions (id, organisation_id, policy_gate_id, eval_id) "
                "VALUES (:id, :oid, :pgid, :eid)"
            ),
            {"id": str(decision_id), "oid": str(org_id), "pgid": str(gate_id), "eid": str(eval_id)},
        )
    return decision_id


# ---------------------------------------------------------------------------
# C4: Partial unique index (at most one live gate per eval_id)
# ---------------------------------------------------------------------------


class TestC4PartialUniqueIndex:
    @pytest.mark.asyncio
    async def test_second_live_insert_rejected(self, db_engine: AsyncEngine) -> None:
        org_id, acc_id, pipe_id = await _setup_org(db_engine)
        node_id = uuid.uuid4()
        eval_id = await _insert_eval(db_engine, org_id, pipe_id, acc_id, node_id)
        await _insert_gate(db_engine, org_id, eval_id, node_id)
        with pytest.raises(IntegrityError):
            await _insert_gate(db_engine, org_id, eval_id, node_id)

    @pytest.mark.asyncio
    async def test_soft_delete_allows_reinsert(self, db_engine: AsyncEngine) -> None:
        org_id, acc_id, pipe_id = await _setup_org(db_engine)
        node_id = uuid.uuid4()
        eval_id = await _insert_eval(db_engine, org_id, pipe_id, acc_id, node_id)
        gate_id = await _insert_gate(db_engine, org_id, eval_id, node_id)
        async with db_engine.begin() as conn:
            await conn.execute(
                text("UPDATE policy_gates SET deleted_at = now() WHERE id = :gid"),
                {"gid": str(gate_id)},
            )
        new_gate = await _insert_gate(db_engine, org_id, eval_id, node_id)
        assert new_gate is not None


# ---------------------------------------------------------------------------
# C12: All THREE composite FKs rejected on cross-organisation inserts
# ---------------------------------------------------------------------------


class TestC12CrossOrgFKRejection:
    @pytest.mark.asyncio
    async def test_gate_cross_org_eval_fk(self, db_engine: AsyncEngine) -> None:
        org_a, acc_a, pipe_a = await _setup_org(db_engine)
        org_b, _, _ = await _setup_org(db_engine)
        node_a = uuid.uuid4()
        eval_a = await _insert_eval(db_engine, org_a, pipe_a, acc_a, node_a)
        with pytest.raises(IntegrityError):
            async with db_engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO policy_gates (id, organisation_id, eval_id, "
                        "node_id, action, version) VALUES (:id, :oid, :eid, :nid, 'warn', 1)"
                    ),
                    {"id": str(uuid.uuid4()), "oid": str(org_b), "eid": str(eval_a), "nid": str(node_a)},
                )

    @pytest.mark.asyncio
    async def test_decision_cross_org_gate_fk_only(self, db_engine: AsyncEngine) -> None:
        """Violates ONLY the (policy_gate_id, org) FK -- eval FK is valid."""
        org_a, acc_a, pipe_a = await _setup_org(db_engine)
        org_b, acc_b, pipe_b = await _setup_org(db_engine)
        node_a = uuid.uuid4()
        node_b = uuid.uuid4()
        eval_a = await _insert_eval(db_engine, org_a, pipe_a, acc_a, node_a)
        gate_a = await _insert_gate(db_engine, org_a, eval_a, node_a)
        # org_b has its own valid eval -- so the eval FK is satisfied.
        eval_b = await _insert_eval(db_engine, org_b, pipe_b, acc_b, node_b)
        with pytest.raises(IntegrityError):
            async with db_engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO policy_gate_decisions (id, organisation_id, policy_gate_id, eval_id) "
                        "VALUES (:id, :oid, :pgid, :eid)"
                    ),
                    # gate_a belongs to org_a -- invalid for org_b's organisation_id.
                    {"id": str(uuid.uuid4()), "oid": str(org_b), "pgid": str(gate_a), "eid": str(eval_b)},
                )

    @pytest.mark.asyncio
    async def test_decision_cross_org_eval_fk_only(self, db_engine: AsyncEngine) -> None:
        """Violates ONLY the (eval_id, org) FK -- gate FK is valid."""
        org_a, acc_a, pipe_a = await _setup_org(db_engine)
        org_b, acc_b, pipe_b = await _setup_org(db_engine)
        node_a = uuid.uuid4()
        node_b = uuid.uuid4()
        eval_a = await _insert_eval(db_engine, org_a, pipe_a, acc_a, node_a)
        # org_b has its own valid gate -- so the gate FK is satisfied.
        eval_b = await _insert_eval(db_engine, org_b, pipe_b, acc_b, node_b)
        gate_b = await _insert_gate(db_engine, org_b, eval_b, node_b)
        with pytest.raises(IntegrityError):
            async with db_engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO policy_gate_decisions (id, organisation_id, policy_gate_id, eval_id) "
                        "VALUES (:id, :oid, :pgid, :eid)"
                    ),
                    # eval_a belongs to org_a -- invalid for org_b's organisation_id.
                    {"id": str(uuid.uuid4()), "oid": str(org_b), "pgid": str(gate_b), "eid": str(eval_a)},
                )


# ---------------------------------------------------------------------------
# C13: BOTH parts for EACH composite FK on policy_gate_decisions
# ---------------------------------------------------------------------------


async def _get_fk_confdeltype(
    conn,
    child_table: str,
    child_columns: tuple[str, str],
    parent_table: str,
):
    """Look up confdeltype for a composite FK by column set + parent table.

    Returns the confdeltype character ('r' for RESTRICT, 'd' for CASCADE, etc.)
    or raises AssertionError with a clear diagnostic message.

    Table and column names are hardcoded constants, so f-string interpolation
    is safe here (not user input).
    """
    col_a, col_b = child_columns
    # Use f-string for table/column names (constants) to avoid ::regclass
    # conflicting with SQLAlchemy bind-parameter syntax.
    sql = (
        f"SELECT confdeltype FROM pg_constraint "  # noqa: S608 — hardcoded constants
        f"WHERE conrelid = '{child_table}'::regclass "
        f"AND conkey @> ARRAY["
        f"  (SELECT attnum::smallint FROM pg_attribute"
        f"   WHERE attrelid = '{child_table}'::regclass"
        f"   AND attname = '{col_a}'),"
        f"  (SELECT attnum::smallint FROM pg_attribute"
        f"   WHERE attrelid = '{child_table}'::regclass"
        f"   AND attname = '{col_b}')"
        f"] "
        f"AND confrelid = '{parent_table}'::regclass"
    )
    result = await conn.execute(text(sql))
    row = result.fetchone()
    assert row is not None, f"FK ({col_a}, {col_b}) -> {parent_table} not found in pg_constraint"
    val = row[0]
    # asyncpg returns single-char columns as bytes; decode for comparison.
    return val.decode() if isinstance(val, bytes) else val


class TestC13FkDeleteActions:
    @pytest.mark.asyncio
    async def test_gate_fk_is_restrict(self, db_engine: AsyncEngine) -> None:
        async with db_engine.connect() as conn:
            confdeltype = await _get_fk_confdeltype(
                conn,
                child_table="policy_gate_decisions",
                child_columns=("policy_gate_id", "organisation_id"),
                parent_table="policy_gates",
            )
        assert confdeltype == "r", f"Expected RESTRICT (confdeltype='r') for FK -> policy_gates, got '{confdeltype}'"

    @pytest.mark.asyncio
    async def test_eval_fk_is_restrict(self, db_engine: AsyncEngine) -> None:
        async with db_engine.connect() as conn:
            confdeltype = await _get_fk_confdeltype(
                conn,
                child_table="policy_gate_decisions",
                child_columns=("eval_id", "organisation_id"),
                parent_table="evals",
            )
        assert confdeltype == "r", f"Expected RESTRICT (confdeltype='r') for FK -> evals, got '{confdeltype}'"

    @pytest.mark.asyncio
    async def test_delete_gate_rejected_when_decision_exists(self, db_engine: AsyncEngine) -> None:
        org_id, acc_id, pipe_id = await _setup_org(db_engine)
        node_id = uuid.uuid4()
        eval_id = await _insert_eval(db_engine, org_id, pipe_id, acc_id, node_id)
        gate_id = await _insert_gate(db_engine, org_id, eval_id, node_id)
        await _insert_decision(db_engine, org_id, gate_id, eval_id)
        with pytest.raises(IntegrityError):
            async with db_engine.begin() as conn:
                await conn.execute(text("DELETE FROM policy_gates WHERE id = :gid"), {"gid": str(gate_id)})

    @pytest.mark.asyncio
    async def test_delete_eval_rejected_when_decision_exists(self, db_engine: AsyncEngine) -> None:
        org_id, acc_id, pipe_id = await _setup_org(db_engine)
        node_id = uuid.uuid4()
        eval_id = await _insert_eval(db_engine, org_id, pipe_id, acc_id, node_id)
        gate_id = await _insert_gate(db_engine, org_id, eval_id, node_id)
        await _insert_decision(db_engine, org_id, gate_id, eval_id)
        with pytest.raises(IntegrityError):
            async with db_engine.begin() as conn:
                await conn.execute(text("DELETE FROM evals WHERE id = :eid"), {"eid": str(eval_id)})


# ---------------------------------------------------------------------------
# C15a (integration half): pre_version_raw is NULL on insert
# ---------------------------------------------------------------------------


class TestC15aPreVersionRawIntegration:
    @pytest.mark.asyncio
    async def test_eval_pre_version_raw_null_on_insert(self, db_engine: AsyncEngine) -> None:
        org_id, acc_id, pipe_id = await _setup_org(db_engine)
        node_id = uuid.uuid4()
        eval_id = await _insert_eval(db_engine, org_id, pipe_id, acc_id, node_id)
        async with db_engine.connect() as conn:
            result = await conn.execute(
                text("SELECT pre_version_raw FROM evals WHERE id = :eid"),
                {"eid": str(eval_id)},
            )
            row = result.fetchone()
        assert row is not None
        assert row[0] is None

    @pytest.mark.asyncio
    async def test_policy_gate_pre_version_raw_null_on_insert(self, db_engine: AsyncEngine) -> None:
        org_id, acc_id, pipe_id = await _setup_org(db_engine)
        node_id = uuid.uuid4()
        eval_id = await _insert_eval(db_engine, org_id, pipe_id, acc_id, node_id)
        gate_id = await _insert_gate(db_engine, org_id, eval_id, node_id)
        async with db_engine.connect() as conn:
            result = await conn.execute(
                text("SELECT pre_version_raw FROM policy_gates WHERE id = :gid"),
                {"gid": str(gate_id)},
            )
            row = result.fetchone()
        assert row is not None
        assert row[0] is None


# ---------------------------------------------------------------------------
# C16: eval_definitions columns match ORM model (reflection contract)
# ---------------------------------------------------------------------------


class TestC16EvalDefinitionSchemaContract:
    @pytest.mark.asyncio
    async def test_eval_definitions_columns_match_orm(self, db_engine: AsyncEngine) -> None:
        from modulo.db.models.eval_definition import EvalDefinition

        async with db_engine.connect() as connection:
            db_columns = await connection.run_sync(
                lambda sync_conn: {column["name"] for column in inspect(sync_conn).get_columns("eval_definitions")}
            )
        orm_columns = {column.name for column in EvalDefinition.__table__.columns}
        missing_in_db = orm_columns - db_columns
        assert not missing_in_db, (
            f"ORM columns absent from migrated schema: {sorted(missing_in_db)}. "
            f"ORM: {sorted(orm_columns)}; DB: {sorted(db_columns)}"
        )
