"""Integration tests for EvalResult persistence (FAR-971, chunk 2 criteria 10-11).

Runs against a real Postgres (testcontainers) via the integration session
fixtures.  Verifies that EvalResult columns round-trip correctly and that the
FK to eval_definitions is enforced.

Covers criteria:
  10 -- EvalResult columns match the input data (real Postgres).
  11 -- EvalResult FK to eval_definitions is enforced (real Postgres).
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _setup_org(engine: AsyncEngine) -> tuple[uuid.UUID, uuid.UUID]:
    """Insert a fresh org + account and return (org_id, account_id)."""
    org_id = uuid.uuid4()
    slug = f"eval-persist-{org_id.hex[:8]}"
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :n, :s, '{}'::json)"),
            {"id": str(org_id), "n": slug, "s": slug},
        )
        acc_id = uuid.uuid4()
        await conn.execute(
            text(
                "INSERT INTO accounts (id, email, display_name, password_hash, "
                "auth_provider, active) "
                "VALUES (:id, :e, :n, 'hash', 'local', true)"
            ),
            {"id": str(acc_id), "e": f"{slug}@test.com", "n": slug},
        )
    return org_id, acc_id


async def _insert_eval_definition(
    engine: AsyncEngine,
    org_id: uuid.UUID,
    account_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    node_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Insert an eval_definitions row and return its id."""
    eval_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO eval_definitions "
                "(id, organisation_id, account_id, pipeline_id, node_id, name, "
                "eval_type, config_json, failure_behaviour, version) "
                "VALUES (:id, :oid, :aid, :pid, :nid, :name, 'regex', "
                "'{}'::json, 'warn', 1)"
            ),
            {
                "id": str(eval_id),
                "oid": str(org_id),
                "aid": str(account_id),
                "pid": str(pipeline_id),
                "nid": str(node_id) if node_id else None,
                "name": f"test-eval-{eval_id.hex[:8]}",
            },
        )
    return eval_id


async def _insert_pipeline(
    engine: AsyncEngine,
    org_id: uuid.UUID,
    account_id: uuid.UUID,
) -> uuid.UUID:
    """Insert a minimal pipelines row and return its id."""
    pipeline_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO pipelines (id, organisation_id, name, account_id, "
                "max_concurrent_runs, lock_wait_timeout_seconds, "
                "node_timeout_seconds, run_context_defaults, graph_nodes_json) "
                "VALUES (:id, :oid, :n, :aid, 10, 30, 300, '{}'::json, '[]'::json)"
            ),
            {
                "id": str(pipeline_id),
                "oid": str(org_id),
                "n": f"eval-persist-pipe-{pipeline_id.hex[:8]}",
                "aid": str(account_id),
            },
        )
    return pipeline_id


async def _insert_run(
    engine: AsyncEngine,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    snapshot_id: uuid.UUID,
) -> uuid.UUID:
    """Insert a minimal runs row and return its id."""
    run_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO runs (id, organisation_id, pipeline_id, snapshot_id, "
                "langgraph_thread_id, trigger_type, input_hash, run_number) "
                "VALUES (:id, :oid, :pid, :sid, :ltid, 'manual', :ih, 1)"
            ),
            {
                "id": str(run_id),
                "oid": str(org_id),
                "pid": str(pipeline_id),
                "sid": str(snapshot_id),
                "ltid": str(uuid.uuid4()),
                "ih": "0" * 64,
            },
        )
    return run_id


async def _insert_snapshot(
    engine: AsyncEngine,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
) -> uuid.UUID:
    """Insert a minimal pipeline_snapshots row and return its id."""
    snapshot_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO pipeline_snapshots "
                "(id, pipeline_id, organisation_id, snapshot_version, "
                "graph_json, connector_bindings_json, schema_pins_json, "
                "prompt_pins_json, model_backend_pins_json, "
                "run_context_defaults, config_json) "
                "VALUES (:id, :pid, :oid, 1, '{}'::json, '[]'::json, "
                "'[]'::json, '[]'::json, '[]'::json, '{}'::json, '{}'::json)"
            ),
            {"id": str(snapshot_id), "pid": str(pipeline_id), "oid": str(org_id)},
        )
    return snapshot_id


# ---------------------------------------------------------------------------
# Criterion 10: EvalResult columns match the input data (real Postgres)
# ---------------------------------------------------------------------------


class TestC10EvalResultColumnRoundTrip:
    @pytest.mark.asyncio
    async def test_all_columns_round_trip(self, db_engine: AsyncEngine) -> None:
        """Persist an EvalResult and read it back; every column must match."""
        org_id, acc_id = await _setup_org(db_engine)
        pipeline_id = await _insert_pipeline(db_engine, org_id, acc_id)
        snapshot_id = await _insert_snapshot(db_engine, org_id, pipeline_id)
        run_id = await _insert_run(db_engine, org_id, pipeline_id, snapshot_id)
        node_id = uuid.uuid4()
        eval_id = await _insert_eval_definition(db_engine, org_id, acc_id, pipeline_id, node_id)

        eval_result_id = uuid.uuid4()
        expected_version = 3
        expected_passed = False
        expected_score = 0.42
        expected_detail = "chunk-2-persistence-test"

        async with db_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO eval_results "
                    "(id, organisation_id, run_id, node_id, eval_id, "
                    "eval_definition_version, passed, score, detail) "
                    "VALUES (:id, :oid, :rid, :nid, :eid, :edv, :p, :s, :d)"
                ),
                {
                    "id": str(eval_result_id),
                    "oid": str(org_id),
                    "rid": str(run_id),
                    "nid": str(node_id),
                    "eid": str(eval_id),
                    "edv": expected_version,
                    "p": expected_passed,
                    "s": expected_score,
                    "d": expected_detail,
                },
            )

        # Read back
        async with db_engine.connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT organisation_id, run_id, node_id, eval_id, "
                    "eval_definition_version, passed, score, detail "
                    "FROM eval_results WHERE id = :id"
                ),
                {"id": str(eval_result_id)},
            )
            row = result.fetchone()

        assert row is not None, "EvalResult row not found after INSERT"

        assert uuid.UUID(str(row[0])) == org_id, "organisation_id mismatch"
        assert uuid.UUID(str(row[1])) == run_id, "run_id mismatch"
        assert uuid.UUID(str(row[2])) == node_id, "node_id mismatch"
        assert uuid.UUID(str(row[3])) == eval_id, "eval_id mismatch"
        assert row[4] == expected_version, "eval_definition_version mismatch"
        assert row[5] is expected_passed, "passed mismatch"
        assert abs(float(row[6]) - expected_score) < 1e-6, "score mismatch"
        assert row[7] == expected_detail, "detail mismatch"

    @pytest.mark.asyncio
    async def test_null_optional_columns(self, db_engine: AsyncEngine) -> None:
        """Nullable columns (score, detail) are stored as NULL when run_id is set.

        Note: run_id is NOT nullable in practice (the ck_eval_results_run_xor_suite
        CHECK constraint requires exactly one of run_id or suite_run_id to be set).
        We test the other nullable columns: score and detail.
        """
        org_id, acc_id = await _setup_org(db_engine)
        pipeline_id = await _insert_pipeline(db_engine, org_id, acc_id)
        snapshot_id = await _insert_snapshot(db_engine, org_id, pipeline_id)
        run_id = await _insert_run(db_engine, org_id, pipeline_id, snapshot_id)
        node_id = uuid.uuid4()
        eval_id = await _insert_eval_definition(db_engine, org_id, acc_id, pipeline_id, node_id)

        eval_result_id = uuid.uuid4()
        async with db_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO eval_results "
                    "(id, organisation_id, run_id, eval_id, passed) "
                    "VALUES (:id, :oid, :rid, :eid, true)"
                ),
                {
                    "id": str(eval_result_id),
                    "oid": str(org_id),
                    "rid": str(run_id),
                    "eid": str(eval_id),
                },
            )

        async with db_engine.connect() as conn:
            result = await conn.execute(
                text("SELECT score, detail FROM eval_results WHERE id = :id"),
                {"id": str(eval_result_id)},
            )
            row = result.fetchone()

        assert row is not None, "EvalResult row not found after INSERT"
        assert row[0] is None, "score should be NULL"
        assert row[1] is None, "detail should be NULL"


# ---------------------------------------------------------------------------
# Criterion 11: EvalResult FK to eval_definitions is enforced (real Postgres)
# ---------------------------------------------------------------------------


class TestC11EvalResultFKEnforcement:
    @pytest.mark.asyncio
    async def test_insert_rejected_for_nonexistent_eval_id(self, db_engine: AsyncEngine) -> None:
        """Inserting an EvalResult referencing a non-existent eval_id is rejected."""
        org_id, acc_id = await _setup_org(db_engine)
        pipeline_id = await _insert_pipeline(db_engine, org_id, acc_id)
        snapshot_id = await _insert_snapshot(db_engine, org_id, pipeline_id)
        run_id = await _insert_run(db_engine, org_id, pipeline_id, snapshot_id)

        fake_eval_id = uuid.uuid4()

        with pytest.raises(IntegrityError):
            async with db_engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO eval_results "
                        "(id, organisation_id, run_id, eval_id, passed) "
                        "VALUES (:id, :oid, :rid, :eid, true)"
                    ),
                    {
                        "id": str(uuid.uuid4()),
                        "oid": str(org_id),
                        "rid": str(run_id),
                        "eid": str(fake_eval_id),
                    },
                )
