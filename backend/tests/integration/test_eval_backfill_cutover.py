"""Chunk-3 backfill + cutover acceptance tests (FAR-1100, spec §8 criteria 1-10, 19, 20).

Runs the real Alembic ``upgrade`` of revision ``0254_eval_backfill_cutover``
against a *fresh, isolated* live Postgres (a private database cloned from
``template0`` so the shared session schema is never touched — mirrors
``test_migration_0191_bundled_runner_seed_backfill.py``), with a seeded
``eval_definitions`` population covering every backfill population:

* an ordinary node-scoped non-guardrail ``warn`` definition (candidate);
* a node-scoped non-guardrail ``block`` definition (candidate);
* a guardrail-typed node-scoped definition (Eval yes, PolicyGate NO);
* a suite-scoped definition (``node_id IS NULL``) (Eval yes, PolicyGate NO);
* a soft-deleted node-scoped definition (Eval yes, PolicyGate NO).

Proved: backfill completeness + anti-join (C1/C2), population exclusions
(C3/C4/C5), action↔failure_behaviour mapping (C6), idempotent re-execution
(C7), FK repoint (C8), EvalResult integrity (C9), untouched legacy table
(C10), per-field content correctness (C19), and the recreated tenant trigger
resolving against ``evals`` (C20). The downgrade restoring the legacy FK +
trigger targets and dropping the backfilled rows is proved too (§5 rollback
contract).
"""

import json
import types
import uuid
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

pytestmark = [pytest.mark.integration]

BACKEND_ROOT = Path(__file__).parents[2]  # backend/
MIGRATION_REV = "0254_eval_backfill_cutover"
PREV_REV = "0253_runs_enforcement_mode_outcome"

_NOW = "now()"


def _alembic_config(db_url: str) -> Config:
    config = Config(BACKEND_ROOT / "alembic.ini")
    config.set_main_option("sqlalchemy.url", db_url)
    config.set_main_option(
        "script_location",
        str(BACKEND_ROOT / "src" / "modulo" / "db" / "migrations"),
    )
    config.config_file_name = None
    return config


def _swap_db_name(db_url: str, new_db: str) -> str:
    from urllib.parse import urlparse, urlunparse

    parsed = urlparse(db_url)
    return urlunparse(parsed._replace(path=f"/{new_db}"))


@pytest_asyncio.fixture
async def isolated_db_url(db_url: str, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[str]:
    """A fresh, private Postgres database migrated only up to ``PREV_REV``.

    Mirrors ``test_migration_0191_bundled_runner_seed_backfill.py``: a private
    database cloned from ``template0`` so this test's upgrade + downgrade never
    mutates the shared session schema. ``env.py`` resolves the target DB from
    ``DATABASE_URL`` / ``DATABASE_ADMIN_URL``, so both are pinned.
    """
    admin_engine = create_async_engine(db_url, poolclass=NullPool, execution_options={"isolation_level": "AUTOCOMMIT"})
    db_name = f"m0254_iso_{uuid.uuid4().hex[:10]}"
    async with admin_engine.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{db_name}" WITH TEMPLATE template0'))
    await admin_engine.dispose()

    iso_url = _swap_db_name(db_url, db_name)
    monkeypatch.setenv("DATABASE_URL", iso_url)
    monkeypatch.setenv("DATABASE_ADMIN_URL", iso_url)
    eng = create_async_engine(iso_url, poolclass=NullPool)
    async with eng.connect() as conn:
        await conn.execute(
            text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(255) NOT NULL PRIMARY KEY)")
        )
        await conn.commit()
    await eng.dispose()

    with pytest.MonkeyPatch().context() as mp:
        mp.setenv("DATABASE_URL", iso_url)
        mp.setenv("DATABASE_ADMIN_URL", iso_url)
        command.upgrade(_alembic_config(iso_url), PREV_REV)

    yield iso_url

    admin_engine = create_async_engine(db_url, poolclass=NullPool, execution_options={"isolation_level": "AUTOCOMMIT"})
    async with admin_engine.connect() as conn:
        await conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
    await admin_engine.dispose()


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


async def _seed_org_and_pipeline(
    engine: AsyncEngine,
) -> dict[str, uuid.UUID]:
    """Create an org + admin account + pipeline; return their ids."""
    org_id = uuid.uuid4()
    account_id = uuid.uuid4()
    pipeline_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO organisations (id, name, slug, settings_json, created_by) "
                "VALUES (:o, 'm0254-org', 'm0254-org', '{}'::json, :a)"
            ),
            {"o": str(org_id), "a": str(account_id)},
        )
        await conn.execute(
            text(
                "INSERT INTO accounts (id, email, display_name, password_hash, auth_provider, active) "
                "VALUES (:a, 'm0254@example.com', 'm0254', 'hash', 'local', true)"
            ),
            {"a": str(account_id)},
        )
        await conn.execute(
            text("INSERT INTO org_memberships (id, account_id, organisation_id, role) VALUES (:m, :a, :o, 'admin')"),
            {"m": str(uuid.uuid4()), "a": str(account_id), "o": str(org_id)},
        )
        await conn.execute(
            text(
                "INSERT INTO pipelines (id, organisation_id, name, account_id, "
                "max_concurrent_runs, lock_wait_timeout_seconds, node_timeout_seconds, "
                "run_context_defaults, graph_nodes_json, default_autonomy_level, visibility) "
                "VALUES (:id, :oid, 'Pipe0254', :uid, 10, 30, 300, "
                "'{}'::json, '[]'::json, 'manual_approval', 'org')"
            ),
            {"id": str(pipeline_id), "oid": str(org_id), "uid": str(account_id)},
        )
    return {"org_id": org_id, "account_id": account_id, "pipeline_id": pipeline_id}


async def _seed_eval_definitions(engine: AsyncEngine, seeded: dict[str, uuid.UUID]) -> dict[str, uuid.UUID]:
    """Seed six eval_definitions rows — one per backfill population.

    Population 5 (dual-scoped: node_id IS NOT NULL AND suite_id IS NOT NULL)
    MUST produce a PolicyGate — it participates in per-node evaluation.

    Returns the definition ids keyed by population for targeted assertions.
    """
    org_id = seeded["org_id"]
    account_id = seeded["account_id"]
    pipeline_id = seeded["pipeline_id"]
    ordinary_id = uuid.uuid4()
    block_id = uuid.uuid4()
    guardrail_id = uuid.uuid4()
    suite_scoped_id = uuid.uuid4()
    soft_deleted_id = uuid.uuid4()
    dual_scoped_id = uuid.uuid4()
    rows = [
        # (id, node_id, name, eval_type, failure_behaviour, suite_id, pass_threshold, deleted)
        (ordinary_id, str(uuid.uuid4()), "ordinary-warn", "regex", "warn", None, None, False),
        (block_id, str(uuid.uuid4()), "ordinary-block", "regex", "block", None, None, False),
        (guardrail_id, str(uuid.uuid4()), "guardrail-def", "guardrail", "warn", None, None, False),
        (suite_scoped_id, None, "suite-scoped-def", "regex", "warn", "legacy-suite", Decimal("0.5000"), False),
        (soft_deleted_id, str(uuid.uuid4()), "soft-deleted-def", "regex", "warn", None, None, True),
        # Population 5: dual-scoped (node_id IS NOT NULL AND suite_id IS NOT NULL).
        # This MUST produce a PolicyGate — per-node evaluation applies.
        (
            dual_scoped_id,
            str(uuid.uuid4()),
            "dual-scoped-def",
            "regex",
            "warn",
            "legacy-suite",
            Decimal("0.7500"),
            False,
        ),
    ]
    async with engine.begin() as conn:
        for eval_id, node_id, name, eval_type, behaviour, suite_id, threshold, deleted in rows:
            await conn.execute(
                text(
                    "INSERT INTO eval_definitions (id, organisation_id, pipeline_id, node_id, name, eval_type, "
                    "config_json, failure_behaviour, pass_threshold, suite_id, account_id, deleted_at, deleted_by) "
                    "VALUES (:id, :oid, :pid, CAST(:nid AS uuid), :name, :etype, CAST(:cfg AS json), "
                    ":fb, :thr, :suite, "
                    ":aid, " + ("now(), :aid2" if deleted else "NULL, NULL") + ")"
                ),
                {
                    "id": str(eval_id),
                    "oid": str(org_id),
                    "pid": str(pipeline_id),
                    "nid": node_id,
                    "name": name,
                    "etype": eval_type,
                    "cfg": '{"pattern": "x"}',
                    "fb": behaviour,
                    "thr": threshold,
                    "suite": suite_id,
                    "aid": str(account_id),
                    "aid2": str(account_id),
                },
            )
    return {
        "ordinary": ordinary_id,
        "block": block_id,
        "guardrail": guardrail_id,
        "suite_scoped": suite_scoped_id,
        "soft_deleted": soft_deleted_id,
        "dual_scoped": dual_scoped_id,
    }


async def _seed_snapshot(engine: AsyncEngine, org_id: uuid.UUID, pipeline_id: uuid.UUID) -> uuid.UUID:
    """Minimal pipeline_snapshots row (mirrors test_eval_cutover_e2e seeding)."""
    snapshot_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO pipeline_snapshots (id, pipeline_id, organisation_id, "
                "snapshot_version, graph_json, connector_bindings_json, "
                "schema_pins_json, prompt_pins_json, model_backend_pins_json, "
                "run_context_defaults, config_json) "
                "VALUES (:id, :pid, :oid, 1, CAST(:graph AS json), '[]'::json, "
                "'[]'::json, '[]'::json, '[]'::json, '{}'::json, '{}'::json)"
            ),
            {
                "id": str(snapshot_id),
                "pid": str(pipeline_id),
                "oid": str(org_id),
                "graph": json.dumps({"nodes": [], "edges": []}),
            },
        )
    return snapshot_id


async def _seed_run(
    engine: AsyncEngine,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    status: str = "complete",
) -> uuid.UUID:
    """Minimal runs row (mirrors test_eval_cutover_e2e seeding).

    Defaults to the TERMINAL ``complete`` status: migration 0254's Step 0 drain
    check treats ``running`` runs as in-flight and would poll for the full drain
    timeout — a pre-migration eval result belongs to a finished run, so a
    terminal status is both realistic and drain-safe.  Callers proving the drain
    scope pass a non-terminal ``status`` explicitly.
    """
    run_id = uuid.uuid4()
    run_number = int(run_id.int % 10**9) + 1
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO runs (id, organisation_id, pipeline_id, snapshot_id, "
                "trigger_type, input_hash, input_payload, langgraph_thread_id, "
                "run_number, status) "
                "VALUES (:id, :oid, :pid, :sid, 'manual', :ih, '{}'::json, :thread, :rn, :status)"
            ),
            {
                "id": str(run_id),
                "oid": str(org_id),
                "pid": str(pipeline_id),
                "sid": str(snapshot_id),
                "ih": uuid.uuid4().hex,
                "thread": f"{org_id}:{run_id}",
                "rn": run_number,
                "status": status,
            },
        )
    return run_id


async def _seed_eval_result(engine: AsyncEngine, seeded: dict[str, uuid.UUID], eval_id: uuid.UUID) -> uuid.UUID:
    """Insert one pre-existing eval_results row referencing *eval_id*.

    Runs at ``PREV_REV`` where the FK still targets ``eval_definitions`` —
    this is the pre-migration result whose integrity must survive the cutover.
    ``ck_eval_results_run_xor_suite`` requires exactly one of run_id /
    suite_run_id, so a minimal snapshot + run row is seeded first.
    """
    snapshot_id = await _seed_snapshot(engine, seeded["org_id"], seeded["pipeline_id"])
    run_id = await _seed_run(engine, seeded["org_id"], seeded["pipeline_id"], snapshot_id)
    result_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO eval_results (id, organisation_id, run_id, eval_id, passed, score, detail) "
                "VALUES (:id, :oid, :rid, :eid, true, 1.0, 'pre-migration result')"
            ),
            {
                "id": str(result_id),
                "oid": str(seeded["org_id"]),
                "rid": str(run_id),
                "eid": str(eval_id),
            },
        )
    return result_id


# ---------------------------------------------------------------------------
# Shared assertions
# ---------------------------------------------------------------------------


async def _scalar(engine: AsyncEngine, sql: str, params: dict | None = None) -> object:
    async with engine.connect() as conn:
        return (await conn.execute(text(sql), params or {})).scalar()


async def _assert_c1_c2_c3_c4_c5(engine: AsyncEngine, seeded: dict[str, uuid.UUID], defs: dict[str, uuid.UUID]) -> None:
    """Backfill completeness: row counts + anti-join + population exclusions."""
    eval_count = await _scalar(engine, "SELECT COUNT(*) FROM evals")
    ed_count = await _scalar(engine, "SELECT COUNT(*) FROM eval_definitions")
    assert eval_count == 6, f"expected 6 Eval rows after 1:1 backfill, got {eval_count}"
    assert ed_count == 6, f"expected 6 source eval_definitions rows, got {ed_count}"

    eligible = await _scalar(
        engine,
        "SELECT COUNT(*) FROM eval_definitions WHERE deleted_at IS NULL "
        "AND node_id IS NOT NULL AND eval_type != 'guardrail'",
    )
    pg_count = await _scalar(engine, "SELECT COUNT(*) FROM policy_gates")
    assert pg_count == 3, (
        f"expected PolicyGate for the 3 live node-scoped non-guardrail candidates "
        f"(ordinary-warn, ordinary-block, dual-scoped), got {pg_count}"
    )
    assert eligible == 3, f"candidate count drifted, got {eligible}"

    # Anti-join: every eligible definition produced exactly one gate (a
    # duplicate + missing pair would keep the COUNT equal but fail this).
    missing = await _scalar(
        engine,
        "SELECT COUNT(*) FROM eval_definitions ed LEFT JOIN policy_gates pg ON pg.eval_id = ed.id "
        "WHERE pg.id IS NULL AND ed.deleted_at IS NULL AND ed.node_id IS NOT NULL AND ed.eval_type != 'guardrail'",
    )
    assert missing == 0, f"anti-join failed: {missing} eligible definition(s) missing a PolicyGate"

    # C3: no PolicyGate for soft-deleted Evals.
    soft_deleted_gates = await _scalar(
        engine,
        "SELECT COUNT(*) FROM policy_gates pg JOIN evals e ON pg.eval_id = e.id WHERE e.deleted_at IS NOT NULL",
    )
    assert soft_deleted_gates == 0, f"{soft_deleted_gates} PolicyGate(s) linked to soft-deleted Eval(s)"

    # C4: no PolicyGate for guardrail-typed Evals.
    guardrail_gates = await _scalar(
        engine,
        "SELECT COUNT(*) FROM policy_gates pg JOIN evals e ON pg.eval_id = e.id WHERE e.eval_type = 'guardrail'",
    )
    assert guardrail_gates == 0, f"{guardrail_gates} PolicyGate(s) linked to guardrail-typed Eval(s)"

    # C5: no PolicyGate for suite-scoped Evals (node_id IS NULL).
    suite_gates = await _scalar(
        engine,
        "SELECT COUNT(*) FROM policy_gates pg JOIN evals e ON pg.eval_id = e.id WHERE e.node_id IS NULL",
    )
    assert suite_gates == 0, f"{suite_gates} PolicyGate(s) linked to suite-scoped Eval(s)"

    # The guardrail / suite-scoped / soft-deleted Evals exist (backfilled) but carry no gate.
    for key in ("guardrail", "suite_scoped", "soft_deleted"):
        eval_id = defs[key]
        eval_exists = await _scalar(engine, "SELECT COUNT(*) FROM evals WHERE id = :eid", {"eid": str(eval_id)})
        gate_exists = await _scalar(
            engine, "SELECT COUNT(*) FROM policy_gates WHERE eval_id = :eid", {"eid": str(eval_id)}
        )
        assert eval_exists == 1, f"Eval row for {key} population missing from evals"
        assert gate_exists == 0, f"PolicyGate must not exist for {key} population"

    # Population 5 (dual-scoped): node_id IS NOT NULL AND suite_id IS NOT NULL.
    # This MUST produce a PolicyGate — per-node evaluation applies.
    dual_eval_id = defs["dual_scoped"]
    dual_gate = await _scalar(
        engine, "SELECT COUNT(*) FROM policy_gates WHERE eval_id = :eid", {"eid": str(dual_eval_id)}
    )
    assert dual_gate == 1, f"dual-scoped eval (node_id + suite_id both set) must produce a PolicyGate, got {dual_gate}"


async def _assert_c6(engine: AsyncEngine) -> None:
    """C6: every PolicyGate.action equals the source failure_behaviour."""
    mismatches = await _scalar(
        engine,
        "SELECT COUNT(*) FROM policy_gates pg JOIN eval_definitions ed ON pg.eval_id = ed.id "
        "WHERE pg.action != ed.failure_behaviour",
    )
    assert mismatches == 0, f"{mismatches} PolicyGate(s) whose action mismatches the source failure_behaviour"

    block_action = await _scalar(
        engine,
        "SELECT pg.action FROM policy_gates pg JOIN eval_definitions ed ON pg.eval_id = ed.id "
        "WHERE ed.failure_behaviour = 'block'",
    )
    assert block_action == "block", f"the block candidate must produce a block gate, got {block_action}"


async def _assert_c8(engine: AsyncEngine) -> None:
    """C8: the FK on eval_results.eval_id targets evals; the legacy target is gone."""
    new_fk = await _scalar(
        engine,
        "SELECT COUNT(*) FROM pg_constraint WHERE conrelid = 'eval_results'::regclass "
        "AND confrelid = 'evals'::regclass",
    )
    assert new_fk == 1, f"expected exactly one eval_results FK referencing evals, got {new_fk}"
    old_fk = await _scalar(
        engine,
        "SELECT COUNT(*) FROM pg_constraint WHERE conrelid = 'eval_results'::regclass "
        "AND confrelid = 'eval_definitions'::regclass",
    )
    assert old_fk == 0, f"legacy FK to eval_definitions must be gone, found {old_fk}"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_upgrade_backfill_completeness_and_populations(isolated_db_url: str) -> None:
    """Criteria 1-5: 1:1 Eval backfill, gated candidates only, anti-join, and
    no PolicyGate for soft-deleted / guardrail / suite-scoped populations."""
    db_url = isolated_db_url
    engine = create_async_engine(db_url, poolclass=NullPool)
    try:
        seeded = await _seed_org_and_pipeline(engine)
        defs = await _seed_eval_definitions(engine, seeded)

        command.upgrade(_alembic_config(db_url), MIGRATION_REV)

        await _assert_c1_c2_c3_c4_c5(engine, seeded, defs)
        await _assert_c6(engine)
        await _assert_c8(engine)
    finally:
        await engine.dispose()


async def test_drain_gate_ignores_non_executing_runs(isolated_db_url: str) -> None:
    """Step 0 drain scope: non-executing non-terminal runs never block the cutover.

    Regression for the 2026-09-22 production deploy wedge: a live database
    carrying parked/recovery runs (``awaiting_human``, ``hitl_parked``,
    ``claimed``, ``unknown``, ``pending``) must still migrate — the drain wait
    gates on ``running`` runs only.  Under the pre-fix gate this test polled for
    the full 600s drain timeout and aborted.
    """
    db_url = isolated_db_url
    engine = create_async_engine(db_url, poolclass=NullPool)
    try:
        seeded = await _seed_org_and_pipeline(engine)
        await _seed_eval_definitions(engine, seeded)
        snapshot_id = await _seed_snapshot(engine, seeded["org_id"], seeded["pipeline_id"])
        for status in ("awaiting_human", "hitl_parked", "claimed", "unknown", "pending"):
            await _seed_run(engine, seeded["org_id"], seeded["pipeline_id"], snapshot_id, status=status)

        command.upgrade(_alembic_config(db_url), MIGRATION_REV)

        # The migration completed despite 5 non-executing non-terminal runs.
        await _assert_c8(engine)
        ed_count = await _scalar(engine, "SELECT COUNT(*) FROM eval_definitions")
        eval_count = await _scalar(engine, "SELECT COUNT(*) FROM evals")
        assert eval_count == ed_count == 6, (
            f"backfill must complete with parked runs present: evals={eval_count}, eval_definitions={ed_count}"
        )
    finally:
        await engine.dispose()


async def test_upgrade_is_idempotent_on_existing_backfill(isolated_db_url: str) -> None:
    """Criterion 7: re-running the migration against an already-backfilled
    database produces identical state — zero new rows, same ids, same gates.

    The re-execution is forced by rewinding ``alembic_version`` to PREV_REV so
    alembic genuinely executes 0254's ``upgrade()`` a second time (a plain
    ``command.upgrade`` at head would be a version-table no-op).
    """
    db_url = isolated_db_url
    engine = create_async_engine(db_url, poolclass=NullPool)
    try:
        seeded = await _seed_org_and_pipeline(engine)
        defs = await _seed_eval_definitions(engine, seeded)
        command.upgrade(_alembic_config(db_url), MIGRATION_REV)

        async with engine.connect() as conn:
            evals_before = (await conn.execute(text("SELECT id FROM evals ORDER BY id"))).fetchall()
            gates_before = (await conn.execute(text("SELECT id FROM policy_gates ORDER BY id"))).fetchall()
            violations_before = (await conn.execute(text("SELECT COUNT(*) FROM eval_backfill_violations"))).scalar()

        # Force a genuine second execution of 0254's upgrade().
        config = _alembic_config(db_url)
        async with engine.begin() as conn:
            await conn.execute(text("UPDATE alembic_version SET version_num = :rev"), {"rev": PREV_REV})
        command.upgrade(config, MIGRATION_REV)

        async with engine.connect() as conn:
            evals_after = (await conn.execute(text("SELECT id FROM evals ORDER BY id"))).fetchall()
            gates_after = (await conn.execute(text("SELECT id FROM policy_gates ORDER BY id"))).fetchall()
            violations_after = (await conn.execute(text("SELECT COUNT(*) FROM eval_backfill_violations"))).scalar()
            ed_count = (await conn.execute(text("SELECT COUNT(*) FROM eval_definitions"))).scalar()

        assert evals_after == evals_before, "re-running the backfill must not insert or duplicate Eval rows"
        assert gates_after == gates_before, "re-running the backfill must not insert or duplicate PolicyGate rows"
        assert violations_after == violations_before, "re-run must not add or remove inventory rows"
        assert violations_before == 0, "violation inventory must stay empty on a clean re-run"
        assert ed_count == 6, f"legacy table untouched by re-run, got {ed_count}"

        await _assert_c1_c2_c3_c4_c5(engine, seeded, defs)
        await _assert_c8(engine)
    finally:
        await engine.dispose()


async def test_fk_repointed_and_eval_result_integrity_preserved(isolated_db_url: str) -> None:
    """Criteria 8-9: the FK targets evals (old target gone) and every
    pre-existing EvalResult.eval_id still resolves (1:1 UUID reuse)."""
    db_url = isolated_db_url
    engine = create_async_engine(db_url, poolclass=NullPool)
    try:
        seeded = await _seed_org_and_pipeline(engine)
        defs = await _seed_eval_definitions(engine, seeded)
        result_id = await _seed_eval_result(engine, seeded, defs["ordinary"])

        command.upgrade(_alembic_config(db_url), MIGRATION_REV)

        await _assert_c8(engine)

        # C9: every eval_results.eval_id resolves against evals.
        dangling = await _scalar(
            engine,
            "SELECT COUNT(*) FROM eval_results er LEFT JOIN evals e ON er.eval_id = e.id WHERE e.id IS NULL",
        )
        assert dangling == 0, f"{dangling} eval_results row(s) no longer resolve against evals"

        # The pre-migration result row survived the migration itself.
        survived = await _scalar(engine, "SELECT COUNT(*) FROM eval_results WHERE id = :rid", {"rid": str(result_id)})
        assert survived == 1, "pre-migration EvalResult row must survive the backfill migration"
    finally:
        await engine.dispose()


async def test_eval_definitions_table_unchanged(isolated_db_url: str) -> None:
    """Criterion 10: the legacy table is NOT altered — its reflected column set
    matches the ORM model exactly, and the audit table exists."""
    from modulo.db.models.eval_definition import EvalDefinition

    db_url = isolated_db_url
    engine = create_async_engine(db_url, poolclass=NullPool)
    try:
        seeded = await _seed_org_and_pipeline(engine)
        await _seed_eval_definitions(engine, seeded)
        command.upgrade(_alembic_config(db_url), MIGRATION_REV)

        async with engine.connect() as conn:
            reflected = await conn.run_sync(lambda sync_conn: inspect(sync_conn).get_columns("eval_definitions"))
        reflected_names = {c["name"] for c in reflected}
        model_names = {c.name for c in EvalDefinition.__table__.columns}

        assert reflected_names == model_names, (
            f"eval_definitions must be untouched by the backfill; reflected-only="
            f"{reflected_names - model_names}, model-only={model_names - reflected_names}"
        )

        audit_exists = await _scalar(
            engine,
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = 'eval_backfill_violations'",
        )
        assert audit_exists == 1, "eval_backfill_violations audit table must exist after the migration"
    finally:
        await engine.dispose()


async def test_content_correctness_every_copied_field_matches_source(isolated_db_url: str) -> None:
    """Criterion 19: for every evals row, each copied field is identical to the
    source eval_definitions row (independent per-field join, not the
    migration's own verification helper)."""
    db_url = isolated_db_url
    engine = create_async_engine(db_url, poolclass=NullPool)
    try:
        seeded = await _seed_org_and_pipeline(engine)
        await _seed_eval_definitions(engine, seeded)
        command.upgrade(_alembic_config(db_url), MIGRATION_REV)

        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT e.id, e.pipeline_id, e.node_id, e.eval_type, e.config_json, e.pass_threshold, "
                        "e.suite_id, e.deleted_at, e.name, e.account_id, e.version, "
                        "ed.pipeline_id, ed.node_id, ed.eval_type, ed.config_json, ed.pass_threshold, "
                        "ed.suite_id, ed.deleted_at, ed.name, ed.account_id, ed.version "
                        "FROM evals e JOIN eval_definitions ed ON e.id = ed.id"
                    )
                )
            ).fetchall()

        assert len(rows) == 6, f"expected 6 joined pairs, got {len(rows)}"
        for row in rows:
            (
                eid,
                e_pipeline,
                e_node,
                e_type,
                e_config,
                e_threshold,
                e_suite,
                e_deleted,
                e_name,
                e_account,
                e_version,
                d_pipeline,
                d_node,
                d_type,
                d_config,
                d_threshold,
                d_suite,
                d_deleted,
                d_name,
                d_account,
                d_version,
            ) = row
            mismatches = []
            if e_pipeline != d_pipeline:
                mismatches.append("pipeline_id")
            if e_node != d_node:
                mismatches.append("node_id")
            if e_type != d_type:
                mismatches.append("eval_type")
            if e_config != d_config:
                mismatches.append("config_json")
            if e_threshold != d_threshold:
                mismatches.append("pass_threshold")
            if e_suite != d_suite:
                mismatches.append("suite_id")
            if e_deleted != d_deleted:
                mismatches.append("deleted_at")
            if e_name != d_name:
                mismatches.append("name")
            if e_account != d_account:
                mismatches.append("account_id")
            if e_version != d_version:
                mismatches.append("version")
            assert not mismatches, f"Eval {eid} copied-field mismatches: {mismatches}"

        # The suite-scoped definition's threshold survived the copy.
        suite_threshold = await _scalar(
            engine,
            "SELECT e.pass_threshold FROM evals e JOIN eval_definitions ed ON e.id = ed.id "
            "WHERE ed.suite_id = 'legacy-suite'",
        )
        assert suite_threshold is not None, "suite-scoped Eval must carry its pass_threshold"
        assert float(suite_threshold) == 0.5, f"suite-scoped threshold drifted, got {suite_threshold}"
    finally:
        await engine.dispose()


async def test_tenant_trigger_resolves_via_evals(isolated_db_url: str) -> None:
    """Criterion 20: after the migration the recreated
    ``trg_eval_results_eval_id_tenant`` resolves ``eval_id`` against ``evals``.

    * An eval_results insert whose eval_id exists ONLY in ``evals`` (never in
      ``eval_definitions``) succeeds — the old trigger would have rejected it.
    * An insert whose eval_id exists in NEITHER table fails with the
      cross-organisation tenant violation.
    """
    from sqlalchemy.exc import IntegrityError

    db_url = isolated_db_url
    engine = create_async_engine(db_url, poolclass=NullPool)
    try:
        seeded = await _seed_org_and_pipeline(engine)
        await _seed_eval_definitions(engine, seeded)
        command.upgrade(_alembic_config(db_url), MIGRATION_REV)

        # The trigger definition points at evals.
        trigger_def = await _scalar(
            engine,
            "SELECT pg_get_triggerdef(oid) FROM pg_trigger WHERE tgname = 'trg_eval_results_eval_id_tenant' "
            "AND tgrelid = 'eval_results'::regclass",
        )
        assert trigger_def is not None, "trg_eval_results_eval_id_tenant must exist after the migration"
        assert "evals" in str(trigger_def), f"trigger must resolve via evals, got: {trigger_def}"
        assert "eval_definitions" not in str(trigger_def), (
            f"trigger must NOT resolve via eval_definitions, got: {trigger_def}"
        )

        # An Eval that exists ONLY in evals (never backfilled from
        # eval_definitions) — exactly the post-cutover authoring shape.
        evals_only_id = uuid.uuid4()
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO evals (id, organisation_id, pipeline_id, node_id, name, eval_type, config_json, "
                    "account_id) VALUES (:id, :oid, :pid, CAST(:nid AS uuid), 'post-cutover-eval', 'regex', "
                    "'{}'::jsonb, :aid)"
                ),
                {
                    "id": str(evals_only_id),
                    "oid": str(seeded["org_id"]),
                    "pid": str(seeded["pipeline_id"]),
                    "nid": str(uuid.uuid4()),
                    "aid": str(seeded["account_id"]),
                },
            )
        # ck_eval_results_run_xor_suite requires exactly one of run_id /
        # suite_run_id — one shared run serves both probes below.
        snapshot_id = await _seed_snapshot(engine, seeded["org_id"], seeded["pipeline_id"])
        probe_run_id = await _seed_run(engine, seeded["org_id"], seeded["pipeline_id"], snapshot_id)
        result_id = uuid.uuid4()
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO eval_results (id, organisation_id, run_id, eval_id, passed) "
                    "VALUES (:id, :oid, :rid, :eid, true)"
                ),
                {
                    "id": str(result_id),
                    "oid": str(seeded["org_id"]),
                    "rid": str(probe_run_id),
                    "eid": str(evals_only_id),
                },
            )
        stored = await _scalar(engine, "SELECT COUNT(*) FROM eval_results WHERE id = :rid", {"rid": str(result_id)})
        assert stored == 1, "eval_results insert resolving via evals (only) must succeed"

        # An eval_id existing in NEITHER table must fail the tenant trigger.
        orphan_id = uuid.uuid4()
        with pytest.raises(IntegrityError, match=r"cross-organisation reference from eval_results\.eval_id to evals"):
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO eval_results (id, organisation_id, run_id, eval_id, passed) "
                        "VALUES (:id, :oid, :rid, :eid, true)"
                    ),
                    {
                        "id": str(uuid.uuid4()),
                        "oid": str(seeded["org_id"]),
                        "rid": str(probe_run_id),
                        "eid": str(orphan_id),
                    },
                )
    finally:
        await engine.dispose()


async def test_violation_inventory_empty_for_filtered_candidates(isolated_db_url: str) -> None:
    """Criterion 18 (integration half): the implementation validates the
    FILTERED candidate set, so a well-formed backfill records zero violations —
    no false positives in the audit inventory."""
    db_url = isolated_db_url
    engine = create_async_engine(db_url, poolclass=NullPool)
    try:
        seeded = await _seed_org_and_pipeline(engine)
        await _seed_eval_definitions(engine, seeded)
        command.upgrade(_alembic_config(db_url), MIGRATION_REV)

        count = await _scalar(engine, "SELECT COUNT(*) FROM eval_backfill_violations")
        assert count == 0, f"clean backfill must record zero violations, got {count}"
    finally:
        await engine.dispose()


async def test_downgrade_restores_legacy_fk_and_trigger_targets(isolated_db_url: str) -> None:
    """§5 rollback contract: the downgrade restores the tenant trigger and FK
    to ``eval_definitions``, deletes the backfilled rows, drops the audit
    table, and RETAINS the legacy data (pure code revert — no data loss)."""
    db_url = isolated_db_url
    engine = create_async_engine(db_url, poolclass=NullPool)
    try:
        seeded = await _seed_org_and_pipeline(engine)
        defs = await _seed_eval_definitions(engine, seeded)
        result_id = await _seed_eval_result(engine, seeded, defs["ordinary"])
        command.upgrade(_alembic_config(db_url), MIGRATION_REV)

        config = _alembic_config(db_url)
        config.cmd_opts = types.SimpleNamespace(command="downgrade")
        command.downgrade(config, "-1")

        trigger_def = await _scalar(
            engine,
            "SELECT pg_get_triggerdef(oid) FROM pg_trigger WHERE tgname = 'trg_eval_results_eval_id_tenant' "
            "AND tgrelid = 'eval_results'::regclass",
        )
        assert trigger_def is not None, "downgrade must recreate the tenant trigger"
        assert "eval_definitions" in str(trigger_def), (
            f"downgraded trigger must resolve via eval_definitions, got: {trigger_def}"
        )

        restored_fk = await _scalar(
            engine,
            "SELECT COUNT(*) FROM pg_constraint WHERE conrelid = 'eval_results'::regclass "
            "AND confrelid = 'eval_definitions'::regclass",
        )
        assert restored_fk == 1, f"downgrade must restore the FK to eval_definitions, got {restored_fk}"
        evals_fk = await _scalar(
            engine,
            "SELECT COUNT(*) FROM pg_constraint WHERE conrelid = 'eval_results'::regclass "
            "AND confrelid = 'evals'::regclass",
        )
        assert evals_fk == 0, f"downgraded FK to evals must be gone, found {evals_fk}"

        evals_count = await _scalar(engine, "SELECT COUNT(*) FROM evals")
        assert evals_count == 0, f"backfilled Eval rows must be deleted on downgrade, got {evals_count}"
        gates_count = await _scalar(engine, "SELECT COUNT(*) FROM policy_gates")
        assert gates_count == 0, f"backfilled PolicyGate rows must be deleted on downgrade, got {gates_count}"

        audit_table = await _scalar(
            engine,
            "SELECT to_regclass('eval_backfill_violations')",
        )
        assert audit_table is None, "audit table must be dropped on downgrade"

        # Legacy data retained — the rollback is a pure code revert.
        ed_count = await _scalar(engine, "SELECT COUNT(*) FROM eval_definitions")
        assert ed_count == 6, f"legacy eval_definitions data must be retained, got {ed_count}"
        survived = await _scalar(engine, "SELECT COUNT(*) FROM eval_results WHERE id = :rid", {"rid": str(result_id)})
        assert survived == 1, "pre-migration EvalResult row must survive the downgrade"

        head = await _scalar(engine, "SELECT version_num FROM alembic_version")
        assert head == PREV_REV, f"head should reset to {PREV_REV}, got {head}"
    finally:
        await engine.dispose()
