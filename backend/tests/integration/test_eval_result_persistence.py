"""Integration tests for EvalResult persistence (FAR-971, chunk 2).

Runs against a real Postgres (testcontainers) via the integration session
fixtures.  Verifies that EvalResult columns round-trip correctly, the FK to
evals is enforced, and the persist-before-decide production path
(pipeline_engine.eval_persist_order.run_evals_persist_before_decide) writes
rows to the real database before the block/warn decision.

Covers criteria:
  10 -- EvalResult columns match the input data (real Postgres).
  11 -- EvalResult FK to eval_definitions is enforced (real Postgres).
  12 -- Blocking eval persists result before halting (real run + DB).
  13 -- Warn eval persists result and run continues (real run + DB).
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from modulo.core.eval_engine import EvalBlockedError, EvalDefinition, EvalType
from modulo.core.pipeline_engine.eval_persist_order import run_evals_persist_before_decide
from tests.integration.conftest import EvalMirrorDefinition, insert_evals_mirror

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
    name: str | None = None,
    failure_behaviour: str = "warn",
) -> uuid.UUID:
    """Insert an eval_definitions row (and its 1:1 evals mirror) and return its id.

    The same-UUID ``evals`` mirror is required because FAR-1100 chunk 3
    (migration 0254) repointed ``eval_results.eval_id`` to ``evals`` before an
    ``eval_results`` row can reference it; see ``insert_evals_mirror`` in the
    integration conftest.
    """
    eval_id = uuid.uuid4()
    eval_name = name or f"test-eval-{eval_id.hex[:8]}"
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO eval_definitions "
                "(id, organisation_id, account_id, pipeline_id, node_id, name, "
                "eval_type, config_json, failure_behaviour, version) "
                "VALUES (:id, :oid, :aid, :pid, :nid, :name, 'regex', "
                "'{}'::json, :fb, 1)"
            ),
            {
                "id": str(eval_id),
                "oid": str(org_id),
                "aid": str(account_id),
                "pid": str(pipeline_id),
                "nid": str(node_id) if node_id else None,
                "name": eval_name,
                "fb": failure_behaviour,
            },
        )
        await insert_evals_mirror(
            conn,
            EvalMirrorDefinition(
                id=eval_id,
                organisation_id=org_id,
                pipeline_id=pipeline_id,
                name=eval_name,
                eval_type="regex",
                account_id=account_id,
                node_id=node_id,
            ),
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
# Criterion 11: EvalResult FK to evals is enforced (real Postgres)
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


# ---------------------------------------------------------------------------
# Criterion 12: Blocking eval persists result before halting (real run + DB)
#
# Calls the PRODUCTION path (run_evals_persist_before_decide) against real
# Postgres.  A blocking eval with failure_behaviour='block' that fails
# MUST:  (a) raise EvalBlockedError, AND (b) have its EvalResult row
# persisted with passed=False.
# ---------------------------------------------------------------------------


class TestC12BlockingEvalPersistsBeforeHalting:
    @pytest.mark.asyncio
    async def test_blocking_eval_persists_result_then_raises(self, db_engine: AsyncEngine) -> None:
        """Criterion 12: a blocking eval that fails persists its EvalResult
        row with passed=False BEFORE raising EvalBlockedError."""
        org_id, acc_id = await _setup_org(db_engine)
        pipeline_id = await _insert_pipeline(db_engine, org_id, acc_id)
        snapshot_id = await _insert_snapshot(db_engine, org_id, pipeline_id)
        run_id = await _insert_run(db_engine, org_id, pipeline_id, snapshot_id)
        node_id = str(uuid.uuid4())

        # Insert a real eval_definitions row with failure_behaviour='block'
        eval_id = await _insert_eval_definition(
            db_engine,
            org_id,
            acc_id,
            pipeline_id,
            uuid.UUID(node_id),
            name="block-fail-eval",
            failure_behaviour="block",
        )

        eval_def = EvalDefinition(
            id=eval_id,
            org_id=org_id,
            pipeline_id=pipeline_id,
            node_id=node_id,
            name="block-fail-eval",
            eval_type=EvalType.REGEX,
            config={"pattern": "^NEVER_MATCH$", "field": "content"},
            failure_behaviour="block",
            version=1,
        )

        session_factory = async_sessionmaker(db_engine, expire_on_commit=False, autobegin=False)

        # The eval target — content that does NOT match the regex
        eval_target = {"content": "some output that will not match"}

        with pytest.raises(EvalBlockedError) as exc_info:
            await run_evals_persist_before_decide(
                eval_defs=[eval_def],
                resolve_eval_target=lambda _ed: eval_target,
                run_id=run_id,
                org_id=org_id,
                session_factory=session_factory,
                node_id=node_id,
            )

        assert exc_info.value.eval_name == "block-fail-eval"

        # Verify the EvalResult row WAS persisted to the real DB
        async with db_engine.connect() as conn:
            result = await conn.execute(
                text("SELECT eval_id, passed, score FROM eval_results WHERE run_id = :rid AND eval_id = :eid"),
                {"rid": str(run_id), "eid": str(eval_id)},
            )
            row = result.fetchone()

        assert row is not None, "EvalResult row must exist after blocking eval — persist-before-decide violated"
        assert uuid.UUID(str(row[0])) == eval_id, "eval_id mismatch"
        assert row[1] is False, f"passed must be False for a failing block eval, got {row[1]}"

    @pytest.mark.asyncio
    async def test_blocking_eval_persists_even_when_subsequent_eval_would_also_block(
        self, db_engine: AsyncEngine
    ) -> None:
        """Criterion 12 (two evals): the first blocking eval's result is
        persisted even though the function raises EvalBlockedError on that
        eval — the second eval is never reached."""
        org_id, acc_id = await _setup_org(db_engine)
        pipeline_id = await _insert_pipeline(db_engine, org_id, acc_id)
        snapshot_id = await _insert_snapshot(db_engine, org_id, pipeline_id)
        run_id = await _insert_run(db_engine, org_id, pipeline_id, snapshot_id)
        node_id = str(uuid.uuid4())

        eval_id_1 = await _insert_eval_definition(
            db_engine,
            org_id,
            acc_id,
            pipeline_id,
            uuid.UUID(node_id),
            name="first-block",
            failure_behaviour="block",
        )
        eval_id_2 = await _insert_eval_definition(
            db_engine,
            org_id,
            acc_id,
            pipeline_id,
            uuid.UUID(node_id),
            name="second-block",
            failure_behaviour="block",
        )

        eval_defs = [
            EvalDefinition(
                id=eval_id_1,
                org_id=org_id,
                pipeline_id=pipeline_id,
                node_id=node_id,
                name="first-block",
                eval_type=EvalType.REGEX,
                config={"pattern": "^NEVER_MATCH$", "field": "content"},
                failure_behaviour="block",
                version=1,
            ),
            EvalDefinition(
                id=eval_id_2,
                org_id=org_id,
                pipeline_id=pipeline_id,
                node_id=node_id,
                name="second-block",
                eval_type=EvalType.REGEX,
                config={"pattern": "^NEVER_MATCH$", "field": "content"},
                failure_behaviour="block",
                version=1,
            ),
        ]

        session_factory = async_sessionmaker(db_engine, expire_on_commit=False, autobegin=False)

        with pytest.raises(EvalBlockedError) as exc_info:
            await run_evals_persist_before_decide(
                eval_defs=eval_defs,
                resolve_eval_target=lambda _ed: {"content": "no match"},
                run_id=run_id,
                org_id=org_id,
                session_factory=session_factory,
                node_id=node_id,
            )

        # First eval raised the block
        assert exc_info.value.eval_name == "first-block"

        # Only the first eval's result was persisted (second was never reached)
        async with db_engine.connect() as conn:
            result = await conn.execute(
                text("SELECT eval_id FROM eval_results WHERE run_id = :rid ORDER BY eval_id"),
                {"rid": str(run_id)},
            )
            rows = result.fetchall()

        assert len(rows) == 1, f"Only the first eval should be persisted, got {len(rows)}"
        assert uuid.UUID(str(rows[0][0])) == eval_id_1


# ---------------------------------------------------------------------------
# Criterion 13: Warn eval persists result and run continues (real run + DB)
#
# Calls the PRODUCTION path (run_evals_persist_before_decide) against real
# Postgres.  A warn eval with failure_behaviour='warn' that fails MUST:
#   (a) NOT raise any exception (run continues), AND
#   (b) have its EvalResult row persisted with passed=False.
# ---------------------------------------------------------------------------


class TestC13WarnEvalPersistsResultAndRunContinues:
    @pytest.mark.asyncio
    async def test_warn_eval_persists_result_and_does_not_raise(self, db_engine: AsyncEngine) -> None:
        """Criterion 13: a warn eval that fails persists its EvalResult row
        with passed=False and does NOT raise EvalBlockedError."""
        org_id, acc_id = await _setup_org(db_engine)
        pipeline_id = await _insert_pipeline(db_engine, org_id, acc_id)
        snapshot_id = await _insert_snapshot(db_engine, org_id, pipeline_id)
        run_id = await _insert_run(db_engine, org_id, pipeline_id, snapshot_id)
        node_id = str(uuid.uuid4())

        eval_id = await _insert_eval_definition(
            db_engine,
            org_id,
            acc_id,
            pipeline_id,
            uuid.UUID(node_id),
            name="warn-fail-eval",
            failure_behaviour="warn",
        )

        eval_def = EvalDefinition(
            id=eval_id,
            org_id=org_id,
            pipeline_id=pipeline_id,
            node_id=node_id,
            name="warn-fail-eval",
            eval_type=EvalType.REGEX,
            config={"pattern": "^NEVER_MATCH$", "field": "content"},
            failure_behaviour="warn",
            version=1,
        )

        session_factory = async_sessionmaker(db_engine, expire_on_commit=False, autobegin=False)

        # Should NOT raise — warn failures are non-blocking
        results = await run_evals_persist_before_decide(
            eval_defs=[eval_def],
            resolve_eval_target=lambda _ed: {"content": "some output that will not match"},
            run_id=run_id,
            org_id=org_id,
            session_factory=session_factory,
            node_id=node_id,
        )

        # The function returned results (run continued)
        assert "warn-fail-eval" in results
        assert results["warn-fail-eval"].passed is False

        # Verify the EvalResult row WAS persisted to the real DB
        async with db_engine.connect() as conn:
            result = await conn.execute(
                text("SELECT eval_id, passed, score FROM eval_results WHERE run_id = :rid AND eval_id = :eid"),
                {"rid": str(run_id), "eid": str(eval_id)},
            )
            row = result.fetchone()

        assert row is not None, "EvalResult row must exist after warn eval — persist-before-decide violated"
        assert uuid.UUID(str(row[0])) == eval_id, "eval_id mismatch"
        assert row[1] is False, f"passed must be False for a failing warn eval, got {row[1]}"

    @pytest.mark.asyncio
    async def test_warn_eval_persists_then_next_eval_runs(self, db_engine: AsyncEngine) -> None:
        """Criterion 13 (two evals): a failing warn eval persists its result
        and the next eval still runs and persists."""
        org_id, acc_id = await _setup_org(db_engine)
        pipeline_id = await _insert_pipeline(db_engine, org_id, acc_id)
        snapshot_id = await _insert_snapshot(db_engine, org_id, pipeline_id)
        run_id = await _insert_run(db_engine, org_id, pipeline_id, snapshot_id)
        node_id = str(uuid.uuid4())

        eval_id_warn = await _insert_eval_definition(
            db_engine,
            org_id,
            acc_id,
            pipeline_id,
            uuid.UUID(node_id),
            name="warn-fail",
            failure_behaviour="warn",
        )
        eval_id_pass = await _insert_eval_definition(
            db_engine,
            org_id,
            acc_id,
            pipeline_id,
            uuid.UUID(node_id),
            name="pass-eval",
            failure_behaviour="warn",
        )

        eval_defs = [
            EvalDefinition(
                id=eval_id_warn,
                org_id=org_id,
                pipeline_id=pipeline_id,
                node_id=node_id,
                name="warn-fail",
                eval_type=EvalType.REGEX,
                config={"pattern": "^NEVER_MATCH$", "field": "content"},
                failure_behaviour="warn",
                version=1,
            ),
            EvalDefinition(
                id=eval_id_pass,
                org_id=org_id,
                pipeline_id=pipeline_id,
                node_id=node_id,
                name="pass-eval",
                eval_type=EvalType.REGEX,
                config={"pattern": ".", "field": "content"},
                failure_behaviour="warn",
                version=1,
            ),
        ]

        session_factory = async_sessionmaker(db_engine, expire_on_commit=False, autobegin=False)

        results = await run_evals_persist_before_decide(
            eval_defs=eval_defs,
            resolve_eval_target=lambda _ed: {"content": "some output"},
            run_id=run_id,
            org_id=org_id,
            session_factory=session_factory,
            node_id=node_id,
        )

        # Both evals ran
        assert results["warn-fail"].passed is False
        assert results["pass-eval"].passed is True

        # Both EvalResult rows persisted to the real DB
        async with db_engine.connect() as conn:
            result = await conn.execute(
                text("SELECT eval_id, passed FROM eval_results WHERE run_id = :rid ORDER BY eval_id"),
                {"rid": str(run_id)},
            )
            rows = result.fetchall()

        assert len(rows) == 2, f"Both eval results must be persisted, got {len(rows)}"
        persisted_ids = {uuid.UUID(str(r[0])): r[1] for r in rows}
        assert persisted_ids[eval_id_warn] is False, "warn eval must be persisted as failed"
        assert persisted_ids[eval_id_pass] is True, "pass eval must be persisted as passed"

    @pytest.mark.asyncio
    async def test_mixed_block_then_warn_persists_both_before_block_raises(self, db_engine: AsyncEngine) -> None:
        """Criterion 12+13 combined: a passing warn eval followed by a
        failing block eval.  The warn result IS persisted; the block
        result IS persisted; then EvalBlockedError is raised."""
        org_id, acc_id = await _setup_org(db_engine)
        pipeline_id = await _insert_pipeline(db_engine, org_id, acc_id)
        snapshot_id = await _insert_snapshot(db_engine, org_id, pipeline_id)
        run_id = await _insert_run(db_engine, org_id, pipeline_id, snapshot_id)
        node_id = str(uuid.uuid4())

        eval_id_warn = await _insert_eval_definition(
            db_engine,
            org_id,
            acc_id,
            pipeline_id,
            uuid.UUID(node_id),
            name="warn-pass",
            failure_behaviour="warn",
        )
        eval_id_block = await _insert_eval_definition(
            db_engine,
            org_id,
            acc_id,
            pipeline_id,
            uuid.UUID(node_id),
            name="block-fail",
            failure_behaviour="block",
        )

        eval_defs = [
            EvalDefinition(
                id=eval_id_warn,
                org_id=org_id,
                pipeline_id=pipeline_id,
                node_id=node_id,
                name="warn-pass",
                eval_type=EvalType.REGEX,
                config={"pattern": ".", "field": "content"},
                failure_behaviour="warn",
                version=1,
            ),
            EvalDefinition(
                id=eval_id_block,
                org_id=org_id,
                pipeline_id=pipeline_id,
                node_id=node_id,
                name="block-fail",
                eval_type=EvalType.REGEX,
                config={"pattern": "^NEVER_MATCH$", "field": "content"},
                failure_behaviour="block",
                version=1,
            ),
        ]

        session_factory = async_sessionmaker(db_engine, expire_on_commit=False, autobegin=False)

        with pytest.raises(EvalBlockedError) as exc_info:
            await run_evals_persist_before_decide(
                eval_defs=eval_defs,
                resolve_eval_target=lambda _ed: {"content": "some output"},
                run_id=run_id,
                org_id=org_id,
                session_factory=session_factory,
                node_id=node_id,
            )

        assert exc_info.value.eval_name == "block-fail"

        # Both eval results must be persisted
        async with db_engine.connect() as conn:
            result = await conn.execute(
                text("SELECT eval_id, passed FROM eval_results WHERE run_id = :rid ORDER BY eval_id"),
                {"rid": str(run_id)},
            )
            rows = result.fetchall()

        assert len(rows) == 2, f"Both eval results must be persisted, got {len(rows)}"
        persisted_ids = {uuid.UUID(str(r[0])): r[1] for r in rows}
        assert persisted_ids[eval_id_warn] is True, "warn eval must be persisted as passed"
        assert persisted_ids[eval_id_block] is False, "block eval must be persisted as failed"
