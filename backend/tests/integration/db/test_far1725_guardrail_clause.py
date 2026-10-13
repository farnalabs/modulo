"""FAR-1725 — eval_results guardrail exclusion must target the ``evals`` source.

Container-tier (real Postgres via Testcontainers).

Why this exists
---------------
``non_guardrail_eval_results_clause()`` excluded guardrail rows with::

    EvalResult.eval_id NOT IN (SELECT eval_definitions.id
                               WHERE eval_type = 'guardrail')

Since the eval-definition storage cutover (FAR-1100/1101, migration ``0254``)
every eval definition — guardrail included — is written to ``evals`` only, and
``eval_results.eval_id`` carries a composite FK to ``evals(id,
organisation_id)`` (migration ``0294``).  ``eval_definitions`` is a frozen
pre-cutover table that no production write path touches, so the sub-select
finds no post-cutover guardrail id and the exclusion silently no-ops: guardrail
results leak into every normal-eval consumer (dashboard pass rates, run evals,
admin eval dashboard, variants, quality report).  Guardrail ``passed`` semantics
are inverted (a regex ``passed=True`` means the pattern MATCHED — a violation),
so the leak skews ``pass_rate``.

These tests pin the storage model AND the consumer contract:

* the probe tests prove, against real Postgres, where ``eval_id`` points and
  that the current write path leaves no ``eval_definitions`` row behind;
* the regression tests fail on the legacy clause and pass on the fixed one.
"""

import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from modulo.core.eval_engine.eval_definition_write import create_or_update_eval
from modulo.db.crud.eval_run import (
    get_evals_for_runs,
    get_run_evals,
    non_guardrail_eval_results_clause,
)
from modulo.db.models.eval import Eval
from modulo.db.models.eval_result import EvalResult
from modulo.db.soft_delete import register_soft_delete_filter

pytestmark = pytest.mark.integration

_COMPOSITE_FK = "fk_eval_results_eval_org"


# ---------------------------------------------------------------------------
# Seed helpers (own org/pipeline per test — no shared fixtures to collide on)
# ---------------------------------------------------------------------------


async def _seed_org_account_pipeline(engine: AsyncEngine) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Insert a fresh org + account + pipeline; return their ids."""
    org_id = uuid.uuid4()
    account_id = uuid.uuid4()
    pipeline_id = uuid.uuid4()
    slug = f"far1725-{org_id.hex[:8]}"
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :n, :s, '{}'::json)"),
            {"id": str(org_id), "n": slug, "s": slug},
        )
        await conn.execute(
            text(
                "INSERT INTO accounts (id, email, display_name, password_hash, "
                "auth_provider, active) VALUES (:id, :e, :n, 'hash', 'local', true)"
            ),
            {"id": str(account_id), "e": f"{slug}@test.com", "n": slug},
        )
        await conn.execute(
            text(
                "INSERT INTO pipelines (id, organisation_id, name, account_id, "
                "max_concurrent_runs, lock_wait_timeout_seconds, node_timeout_seconds, "
                "run_context_defaults, graph_nodes_json) "
                "VALUES (:id, :oid, :n, :aid, 10, 30, 300, '{}'::json, '[]'::json)"
            ),
            {"id": str(pipeline_id), "oid": str(org_id), "n": slug, "aid": str(account_id)},
        )
    return org_id, account_id, pipeline_id


async def _seed_run(engine: AsyncEngine, org_id: uuid.UUID, pipeline_id: uuid.UUID) -> uuid.UUID:
    """Insert a minimal snapshot + run row and return the run id."""
    snapshot_id = uuid.uuid4()
    run_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO pipeline_snapshots (id, pipeline_id, organisation_id, "
                "snapshot_version, graph_json, connector_bindings_json, "
                "schema_pins_json, prompt_pins_json, model_backend_pins_json, "
                "run_context_defaults, config_json) "
                "VALUES (:id, :pid, :oid, 1, '{}'::json, '[]'::json, '[]'::json, "
                "'[]'::json, '[]'::json, '{}'::json, '{}'::json)"
            ),
            {"id": str(snapshot_id), "pid": str(pipeline_id), "oid": str(org_id)},
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
                "pid": str(pipeline_id),
                "sid": str(snapshot_id),
                "ltid": str(uuid.uuid4()),
                "ih": "0" * 64,
            },
        )
    return run_id


async def _write_eval(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    account_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    eval_type: str,
    name: str,
) -> uuid.UUID:
    """Persist an eval through the CURRENT production write path.

    ``create_or_update_eval`` is the single shared helper every authoring path
    (REST, MCP, feedback publish, guardrail config) funnels through since the
    cutover — using it here is what makes the probe evidence representative of
    production writes rather than of a hand-rolled fixture.
    """
    config: dict[str, object] = {}
    if eval_type == "guardrail":
        config = {"action": "observe", "type": "regex", "pattern": "nope", "interception_point": "input"}
    row = await create_or_update_eval(
        session,
        org_id=org_id,
        account_id=account_id,
        pipeline_id=pipeline_id,
        node_id=None,
        name=name,
        eval_type=eval_type,
        config_json=config,
        failure_behaviour="warn",
        pass_threshold=None,
        suite_id=None,
    )
    return row.id


async def _insert_result(
    engine: AsyncEngine,
    *,
    org_id: uuid.UUID,
    run_id: uuid.UUID,
    eval_id: uuid.UUID,
    passed: bool,
) -> uuid.UUID:
    result_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO eval_results (id, organisation_id, run_id, eval_id, passed) "
                "VALUES (:id, :oid, :rid, :eid, :p)"
            ),
            {"id": str(result_id), "oid": str(org_id), "rid": str(run_id), "eid": str(eval_id), "p": passed},
        )
    return result_id


def _session_maker(engine: AsyncEngine):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    return async_sessionmaker(engine, expire_on_commit=False)


# ---------------------------------------------------------------------------
# Probe: where does eval_id actually point?
# ---------------------------------------------------------------------------


class TestStorageModelProbe:
    async def test_eval_result_fk_targets_evals_not_eval_definitions(self, db_engine: AsyncEngine) -> None:
        """Structural probe: the live FK on eval_results references ``evals``."""
        async with db_engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT c.conname, pg_get_constraintdef(c.oid) FROM pg_constraint c "
                        "WHERE c.conrelid = CAST('eval_results' AS regclass) "
                        "AND c.confrelid = CAST('evals' AS regclass) AND c.contype = 'f'"
                    )
                )
            ).fetchone()
        assert row is not None, "eval_results has no FK to evals — storage model changed"
        assert row[0] == _COMPOSITE_FK, row[1]
        assert "REFERENCES evals(id, organisation_id)" in row[1], row[1]

    async def test_current_write_path_leaves_no_eval_definitions_row(self, db_engine: AsyncEngine) -> None:
        """Behavioural probe: a guardrail written via the production helper
        lands in ``evals`` and nowhere else.

        This is the fact that makes the legacy clause a no-op — it is asserted
        so a future dual-write reinstatement fails here loudly.
        """
        org_id, account_id, pipeline_id = await _seed_org_account_pipeline(db_engine)
        maker = _session_maker(db_engine)
        async with maker() as session:
            guardrail_id = await _write_eval(
                session,
                org_id=org_id,
                account_id=account_id,
                pipeline_id=pipeline_id,
                eval_type="guardrail",
                name="probe-guardrail",
            )
            await session.commit()

        async with db_engine.connect() as conn:
            evals_rows = (
                await conn.execute(text("SELECT eval_type FROM evals WHERE id = :id"), {"id": str(guardrail_id)})
            ).fetchall()
            legacy_rows = (
                await conn.execute(
                    text("SELECT eval_type FROM eval_definitions WHERE id = :id"), {"id": str(guardrail_id)}
                )
            ).fetchall()

        assert [r[0] for r in evals_rows] == ["guardrail"], f"evals row missing: {evals_rows}"
        assert legacy_rows == [], (
            "eval_definitions unexpectedly holds the guardrail id — the storage model "
            "hypothesis (eval_definitions is frozen) is wrong and this fixture is stale"
        )


# ---------------------------------------------------------------------------
# Regression: the exclusion must hold for post-cutover guardrail rows
# ---------------------------------------------------------------------------


class TestGuardrailExclusion:
    async def test_get_run_evals_excludes_post_cutover_guardrail_rows(self, db_engine: AsyncEngine) -> None:
        """THE regression: a guardrail result written after the cutover must not
        surface through ``get_run_evals`` (the MCP run-evals consumer).

        Fails on the legacy ``eval_definitions``-sourced clause: the guardrail
        id is absent from that table, so ``NOT IN`` never matches and both rows
        come back.
        """
        org_id, account_id, pipeline_id = await _seed_org_account_pipeline(db_engine)
        run_id = await _seed_run(db_engine, org_id, pipeline_id)
        maker = _session_maker(db_engine)
        async with maker() as session:
            guardrail_id = await _write_eval(
                session,
                org_id=org_id,
                account_id=account_id,
                pipeline_id=pipeline_id,
                eval_type="guardrail",
                name="gr",
            )
            normal_id = await _write_eval(
                session,
                org_id=org_id,
                account_id=account_id,
                pipeline_id=pipeline_id,
                eval_type="regex",
                name="normal",
            )
            await session.commit()

        # Guardrail "passed" means the pattern MATCHED — a violation, inverted
        # from a normal eval's semantics. It must never reach a pass-rate.
        await _insert_result(db_engine, org_id=org_id, run_id=run_id, eval_id=guardrail_id, passed=True)
        await _insert_result(db_engine, org_id=org_id, run_id=run_id, eval_id=normal_id, passed=True)

        async with maker() as session:
            rows = await get_run_evals(session, run_id)

        assert [r.eval_id for r in rows] == [normal_id], (
            f"guardrail row leaked: expected only {normal_id}, got {[r.eval_id for r in rows]}"
        )

    async def test_get_evals_for_runs_excludes_post_cutover_guardrail_rows(self, db_engine: AsyncEngine) -> None:
        """Same contract on the coverage-gap read-model consumer."""
        org_id, account_id, pipeline_id = await _seed_org_account_pipeline(db_engine)
        run_id = await _seed_run(db_engine, org_id, pipeline_id)
        maker = _session_maker(db_engine)
        async with maker() as session:
            guardrail_id = await _write_eval(
                session,
                org_id=org_id,
                account_id=account_id,
                pipeline_id=pipeline_id,
                eval_type="guardrail",
                name="gr2",
            )
            normal_id = await _write_eval(
                session,
                org_id=org_id,
                account_id=account_id,
                pipeline_id=pipeline_id,
                eval_type="json_schema",
                name="normal2",
            )
            await session.commit()

        await _insert_result(db_engine, org_id=org_id, run_id=run_id, eval_id=guardrail_id, passed=True)
        await _insert_result(db_engine, org_id=org_id, run_id=run_id, eval_id=normal_id, passed=False)

        async with maker() as session:
            rows = await get_evals_for_runs(session, org_id=org_id, run_ids=[run_id])

        assert [r.eval_id for r in rows] == [normal_id], (
            f"guardrail row leaked: expected only {normal_id}, got {[r.eval_id for r in rows]}"
        )

    async def test_soft_deleted_guardrail_results_still_excluded(self, db_engine: AsyncEngine) -> None:
        """Soft-deleting a guardrail must not un-exclude its stored results.

        ``Eval`` is soft-deletable, so an ORM-level sub-select would pick up the
        global ``deleted_at IS NULL`` loader criteria and drop the guardrail id
        from the exclusion set — re-leaking rows whose ``passed`` semantics are
        still inverted. The clause must see soft-deleted definitions too.
        """
        # The soft-delete loader criteria is a process-global listener; register
        # it explicitly so this test exercises the filter deterministically
        # regardless of which other modules the session has imported.
        register_soft_delete_filter()

        org_id, account_id, pipeline_id = await _seed_org_account_pipeline(db_engine)
        run_id = await _seed_run(db_engine, org_id, pipeline_id)
        maker = _session_maker(db_engine)
        async with maker() as session:
            guardrail_id = await _write_eval(
                session,
                org_id=org_id,
                account_id=account_id,
                pipeline_id=pipeline_id,
                eval_type="guardrail",
                name="gr-soft",
            )
            normal_id = await _write_eval(
                session,
                org_id=org_id,
                account_id=account_id,
                pipeline_id=pipeline_id,
                eval_type="regex",
                name="normal-soft",
            )
            await session.commit()

        async with db_engine.begin() as conn:
            await conn.execute(text("UPDATE evals SET deleted_at = now() WHERE id = :id"), {"id": str(guardrail_id)})

        await _insert_result(db_engine, org_id=org_id, run_id=run_id, eval_id=guardrail_id, passed=True)
        await _insert_result(db_engine, org_id=org_id, run_id=run_id, eval_id=normal_id, passed=True)

        async with maker() as session:
            rows = await get_run_evals(session, run_id)

        assert [r.eval_id for r in rows] == [normal_id], (
            f"soft-deleted guardrail leaked: expected only {normal_id}, got {[r.eval_id for r in rows]}"
        )

    async def test_backfilled_guardrail_rows_still_excluded(self, db_engine: AsyncEngine) -> None:
        """No-regression: a pre-cutover guardrail (present in BOTH tables under
        one UUID — the 0254 backfill shape) must stay excluded too."""
        org_id, account_id, pipeline_id = await _seed_org_account_pipeline(db_engine)
        run_id = await _seed_run(db_engine, org_id, pipeline_id)
        legacy_id = uuid.uuid4()
        async with db_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO eval_definitions (id, organisation_id, account_id, pipeline_id, "
                    "node_id, name, eval_type, config_json, failure_behaviour, version) "
                    "VALUES (:id, :oid, :aid, :pid, NULL, 'legacy-gr', 'guardrail', '{}'::json, 'warn', 1)"
                ),
                {"id": str(legacy_id), "oid": str(org_id), "aid": str(account_id), "pid": str(pipeline_id)},
            )
            await conn.execute(
                text(
                    "INSERT INTO evals (id, organisation_id, pipeline_id, node_id, name, eval_type, "
                    "config_json, account_id, version) "
                    "VALUES (:id, :oid, :pid, NULL, 'legacy-gr', 'guardrail', '{}'::json, :aid, 1)"
                ),
                {"id": str(legacy_id), "oid": str(org_id), "pid": str(pipeline_id), "aid": str(account_id)},
            )

        maker = _session_maker(db_engine)
        async with maker() as session:
            normal_id = await _write_eval(
                session,
                org_id=org_id,
                account_id=account_id,
                pipeline_id=pipeline_id,
                eval_type="regex",
                name="normal-legacy",
            )
            await session.commit()

        await _insert_result(db_engine, org_id=org_id, run_id=run_id, eval_id=legacy_id, passed=True)
        await _insert_result(db_engine, org_id=org_id, run_id=run_id, eval_id=normal_id, passed=True)

        async with maker() as session:
            rows = await get_run_evals(session, run_id)

        assert [r.eval_id for r in rows] == [normal_id], (
            f"backfilled guardrail leaked: expected only {normal_id}, got {[r.eval_id for r in rows]}"
        )

    async def test_clause_selects_guardrail_ids_from_evals(self, db_engine: AsyncEngine) -> None:
        """Direct check of the clause's own sub-select, independent of consumers.

        Compiles the exclusion and runs it against a stored guardrail result so
        a future table swap is caught even if every consumer query changes too.
        A result row MUST exist — without one the query is empty for the wrong
        reason and the assertion can never fail.
        """
        org_id, account_id, pipeline_id = await _seed_org_account_pipeline(db_engine)
        run_id = await _seed_run(db_engine, org_id, pipeline_id)
        maker = _session_maker(db_engine)
        async with maker() as session:
            guardrail_id = await _write_eval(
                session,
                org_id=org_id,
                account_id=account_id,
                pipeline_id=pipeline_id,
                eval_type="guardrail",
                name="gr-clause",
            )
            await session.commit()

        await _insert_result(db_engine, org_id=org_id, run_id=run_id, eval_id=guardrail_id, passed=True)

        async with maker() as session:
            guardrail_ids = {
                row[0] for row in await session.execute(select(Eval.id).where(Eval.eval_type == "guardrail"))
            }
            excluded = await session.execute(
                select(EvalResult.eval_id).where(
                    EvalResult.run_id == run_id,
                    non_guardrail_eval_results_clause(),
                )
            )
            surviving = [r[0] for r in excluded.all()]

        assert guardrail_id in guardrail_ids, "guardrail id not visible in the evals guardrail set"
        assert surviving == [], f"clause did not exclude the guardrail row (surviving={surviving})"
