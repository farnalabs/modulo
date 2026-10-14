"""Integration tests for the composite org-scoped FK on ``eval_results`` (FAR-969).

Runs against real Postgres (Testcontainers) via the integration session
fixtures.  ``eval_results.eval_id`` must reference ``evals(id, organisation_id)``
— the composite pair, not a plain ``evals.id`` FK — so a cross-organisation
``eval_id`` is rejected at the DB level even when the same-org tenant trigger is
absent or disabled.

``ck_eval_results_run_xor_suite`` requires exactly one of ``run_id`` /
``suite_run_id``, so every seeded result carries a same-organisation ``run_id``.
That is deliberate: it makes the org FK the ONLY possible cross-org violation,
so a rejection cannot pass for the wrong reason (the CHECK would otherwise fire
first and mask a missing FK).

The trigger ``trg_eval_results_eval_id_tenant`` independently guards same-org
binding, so a cross-org insert also fails through that path.  The decisive test
for the CONSTRAINT itself disables the trigger inside a rolled-back transaction
and asserts the composite FK still rejects the row — a caller that bypasses the
trigger (e.g. ``ALTER TABLE ... DISABLE TRIGGER`` during a bulk load, or a
restored schema) must not be able to write a cross-org reference.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.integration

_COMPOSITE_FK = "fk_eval_results_eval_org"
_OLD_FK = "eval_results_eval_id_fkey"
_TENANT_TRIGGER = "trg_eval_results_eval_id_tenant"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _setup_org(engine: AsyncEngine):
    org_id = uuid.uuid4()
    slug = f"eval-fk-{org_id.hex[:8]}"
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


async def _insert_run(engine: AsyncEngine, org_id, pipe_id) -> uuid.UUID:
    """Seed a minimal snapshot + terminated run and return the run id.

    The run is required by ``ck_eval_results_run_xor_suite``; making it
    same-organisation keeps the run-id tenant trigger satisfied so the eval FK
    is the only guard under test.
    """
    snapshot_id = uuid.uuid4()
    run_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO pipeline_snapshots (id, pipeline_id, organisation_id, "
                "snapshot_version, graph_json, connector_bindings_json, "
                "schema_pins_json, prompt_pins_json, model_backend_pins_json, "
                "run_context_defaults, config_json) "
                "VALUES (:id, :pid, :oid, 1, '{}'::json, '[]'::json, "
                "'[]'::json, '[]'::json, '[]'::json, '{}'::json, '{}'::json)"
            ),
            {"id": str(snapshot_id), "pid": str(pipe_id), "oid": str(org_id)},
        )
        await conn.execute(
            text(
                "INSERT INTO runs (id, organisation_id, pipeline_id, snapshot_id, "
                "langgraph_thread_id, trigger_type, input_hash, run_number) "
                "VALUES (:id, :oid, :pid, :sid, :ltid, 'manual', :ih, 1)"
            ),
            {
                "id": str(run_id),
                "oid": str(org_id),
                "pid": str(pipe_id),
                "sid": str(snapshot_id),
                "ltid": str(uuid.uuid4()),
                "ih": "0" * 64,
            },
        )
    return run_id


async def _insert_eval(engine: AsyncEngine, org_id, pipe_id, acc_id, node_id) -> uuid.UUID:
    eval_id = uuid.uuid4()
    async with engine.begin() as conn:
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
                "nid": str(node_id),
            },
        )
    return eval_id


async def _insert_eval_result(engine: AsyncEngine, org_id, eval_id, run_id, passed: bool = True) -> uuid.UUID:
    result_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO eval_results (id, organisation_id, run_id, eval_id, passed) "
                "VALUES (:id, :oid, :rid, :eid, :passed)"
            ),
            {"id": str(result_id), "oid": str(org_id), "rid": str(run_id), "eid": str(eval_id), "passed": passed},
        )
    return result_id


async def _fk_row(conn, child_table: str, parent_table: str):
    """Return ``(conname, confdeltype, constraint_def)`` for the child→parent FK."""
    result = await conn.execute(
        text(
            "SELECT c.conname, c.confdeltype, pg_get_constraintdef(c.oid) "
            "FROM pg_constraint c "
            "WHERE c.conrelid = CAST(:child AS regclass) "
            "  AND c.confrelid = CAST(:parent AS regclass) "
            "  AND c.contype = 'f'"
        ),
        {"child": child_table, "parent": parent_table},
    )
    row = result.fetchone()
    assert row is not None, f"no FK {child_table} -> {parent_table} found in pg_constraint"
    conname, confdeltype, definition = row
    if isinstance(confdeltype, bytes):
        confdeltype = confdeltype.decode()
    return conname, confdeltype, definition


# ---------------------------------------------------------------------------
# Constraint shape
# ---------------------------------------------------------------------------


class TestCompositeFkShape:
    @pytest.mark.asyncio
    async def test_fk_is_composite_over_eval_id_and_organisation_id(self, db_engine: AsyncEngine) -> None:
        async with db_engine.connect() as conn:
            conname, confdeltype, definition = await _fk_row(conn, "eval_results", "evals")
        assert conname == _COMPOSITE_FK, definition
        assert "FOREIGN KEY (eval_id, organisation_id)" in definition, definition
        assert "REFERENCES evals(id, organisation_id)" in definition, definition
        assert confdeltype == "c", f"expected ON DELETE CASCADE, got confdeltype={confdeltype!r}"

    @pytest.mark.asyncio
    async def test_single_column_legacy_fk_is_gone(self, db_engine: AsyncEngine) -> None:
        async with db_engine.connect() as conn:
            legacy = await conn.execute(
                text("SELECT COUNT(*) FROM pg_constraint WHERE conname = :name"),
                {"name": _OLD_FK},
            )
        assert legacy.scalar_one() == 0, "the single-column eval_results_eval_id_fkey must be dropped"

    @pytest.mark.asyncio
    async def test_evals_unique_id_organisation_id_exists(self, db_engine: AsyncEngine) -> None:
        async with db_engine.connect() as conn:
            unique = await conn.execute(
                text(
                    "SELECT COUNT(*) FROM pg_constraint WHERE conname = 'uq_evals_id_organisation_id' AND contype = 'u'"
                )
            )
        assert unique.scalar_one() == 1, "evals must carry UNIQUE (id, organisation_id) for the composite FK target"


# ---------------------------------------------------------------------------
# Enforcement
# ---------------------------------------------------------------------------


class TestCrossOrgEnforcement:
    @pytest.mark.asyncio
    async def test_same_org_reference_accepted(self, db_engine: AsyncEngine) -> None:
        org_a, acc_a, pipe_a = await _setup_org(db_engine)
        eval_a = await _insert_eval(db_engine, org_a, pipe_a, acc_a, uuid.uuid4())
        run_a = await _insert_run(db_engine, org_a, pipe_a)
        result_id = await _insert_eval_result(db_engine, org_a, eval_a, run_a)
        async with db_engine.connect() as conn:
            stored = await conn.execute(
                text("SELECT organisation_id FROM eval_results WHERE id = :id"),
                {"id": str(result_id)},
            )
        assert stored.scalar_one() == org_a

    @pytest.mark.asyncio
    async def test_cross_org_reference_rejected(self, db_engine: AsyncEngine) -> None:
        org_a, acc_a, pipe_a = await _setup_org(db_engine)
        org_b, _acc_b, pipe_b = await _setup_org(db_engine)
        eval_a = await _insert_eval(db_engine, org_a, pipe_a, acc_a, uuid.uuid4())
        # org_b's own run satisfies the run FK + run tenant trigger, so the
        # eval_id org FK is the only remaining violation.
        run_b = await _insert_run(db_engine, org_b, pipe_b)
        with pytest.raises(IntegrityError):
            await _insert_eval_result(db_engine, org_b, eval_a, run_b)

    @pytest.mark.asyncio
    async def test_cross_org_rejected_by_fk_with_tenant_trigger_disabled(self, db_engine: AsyncEngine) -> None:
        """The composite FK alone must reject the row — prove it with the
        same-org tenant trigger disabled for the duration of a rolled-back txn.

        A caller that disables the trigger (bulk load / restore path) must not
        be able to bind another organisation's ``eval_id``.
        """
        org_a, acc_a, pipe_a = await _setup_org(db_engine)
        org_b, _acc_b, pipe_b = await _setup_org(db_engine)
        eval_a = await _insert_eval(db_engine, org_a, pipe_a, acc_a, uuid.uuid4())
        run_b = await _insert_run(db_engine, org_b, pipe_b)

        async with db_engine.connect() as conn:
            trans = await conn.begin()
            try:
                await conn.execute(text(f'ALTER TABLE eval_results DISABLE TRIGGER "{_TENANT_TRIGGER}"'))
                with pytest.raises(IntegrityError):
                    await conn.execute(
                        text(
                            "INSERT INTO eval_results (id, organisation_id, run_id, eval_id, passed) "
                            "VALUES (:id, :oid, :rid, :eid, true)"
                        ),
                        {"id": str(uuid.uuid4()), "oid": str(org_b), "rid": str(run_b), "eid": str(eval_a)},
                    )
            finally:
                # Rolls back the DISABLE TRIGGER (and any failed insert), restoring
                # the tenant trigger for every later test.
                await trans.rollback()

        # The trigger is active again after the rollback.
        async with db_engine.connect() as conn:
            enabled = await conn.execute(
                text("SELECT tgenabled FROM pg_trigger WHERE tgname = :name AND tgrelid = 'eval_results'::regclass"),
                {"name": _TENANT_TRIGGER},
            )
        assert enabled.scalar_one() != "D", "tenant trigger must be re-enabled after rollback"
