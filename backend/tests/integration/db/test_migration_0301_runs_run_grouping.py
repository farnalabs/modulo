"""Integration test for migration 0301_runs_run_grouping (FAR-153 / §15.1).

Against REAL Postgres (testcontainers, private database per test — the
``test_migration_0215`` / ``test_migration_0283`` harness):

* NEGATIVE CONTROL — at ``0300_benign_facts_repair`` the global
  ``UNIQUE(langgraph_thread_id)`` (``runs_langgraph_thread_id_key``) EXISTS
  and the ``run_group`` columns do NOT;
* the REAL alembic upgrade of ``0301_runs_run_grouping`` over an already
  POPULATED ``runs`` table:
  - both columns land nullable with no server default (metadata-only — the
    seeded rows keep their exact threads and parent link),
  - ``runs_langgraph_thread_id_key`` is dropped,
  - ``ix_runs_run_group_id`` + the two §15.1 partial unique indexes are
    created, their ``pg_indexes.indexdef`` carrying the model's predicates
    (``= 'resume'`` and ``IS DISTINCT FROM 'resume'``);
* behaviour enforced by those indexes on Postgres (not just SQLite):
  - a retry child with a FRESH thread is accepted,
  - a resume child sharing its parent's thread is accepted (this is the
    whole point of dropping the global unique),
  - a SECOND resume of the same parent is rejected with SQLSTATE 23505
    (the DB half of "double-resume rejected 409/DB" — the 409 is the route
    layer's generic IntegrityError mapping when the resume endpoint lands),
  - a resume-of-resume chain is accepted (its parent is a different row),
  - a NON-resume row sharing an existing thread is rejected (global thread
    uniqueness survives for roots/retries/legacy rows);
* the DOWNGRADE refuses with an actionable RuntimeError while resume
  children share threads (data intact, alembic_version unchanged — the
  0151/0298 downgrade-refusal precedent), succeeds once they are removed
  (restoring the global unique and dropping the columns/indexes), and the
  upgrade round-trips cleanly afterwards.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from modulo.db.sqlstates import sqlstate_of

pytestmark = [pytest.mark.integration]

BACKEND_ROOT = Path(__file__).parents[3]  # backend/

MIGRATION_REV = "0301_runs_run_grouping"
PREV_REV = "0300_benign_facts_repair"

_THREAD_CONSTRAINT = "runs_langgraph_thread_id_key"
_NEW_INDEXES = ("ix_runs_run_group_id", "uq_runs_resume_parent", "uq_runs_thread_non_resume")


# ---------------------------------------------------------------------------
# Harness (mirrors test_migration_0283_runs_drop_unused_indexes.py)
# ---------------------------------------------------------------------------


def _alembic_config(db_url: str) -> Config:
    config = Config(BACKEND_ROOT / "alembic.ini")
    config.set_main_option("sqlalchemy.url", db_url)
    config.set_main_option("script_location", str(BACKEND_ROOT / "src" / "modulo" / "db" / "migrations"))
    config.config_file_name = None
    return config


def _swap_db_name(db_url: str, new_db: str) -> str:
    parsed = urlparse(db_url)
    return urlunparse(parsed._replace(path=f"/{new_db}"))


def _alembic_cmd(iso_url: str, cmd: str, target: str) -> None:
    """Run an alembic command against the isolated DB via the Python API.

    Deliberately does NOT inject ``cmd_opts`` (the FAR-967 F2 shape): env.py
    infers direction from the active EnvironmentContext instead.
    """
    cfg = _alembic_config(iso_url)
    assert getattr(cfg, "cmd_opts", None) is None, "Python-API invocation must carry no cmd_opts"
    getattr(command, cmd)(cfg, target)


async def _fresh_db(db_url: str, prefix: str) -> tuple[str, str]:
    """Create an empty private database; return ``(name, async url)``."""
    admin_engine = create_async_engine(db_url, poolclass=NullPool, execution_options={"isolation_level": "AUTOCOMMIT"})
    db_name = f"{prefix}_{uuid.uuid4().hex[:10]}"
    async with admin_engine.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{db_name}" WITH TEMPLATE template0'))
    await admin_engine.dispose()
    return db_name, _swap_db_name(db_url, db_name)


async def _drop_db(db_url: str, db_name: str) -> None:
    admin_engine = create_async_engine(db_url, poolclass=NullPool, execution_options={"isolation_level": "AUTOCOMMIT"})
    async with admin_engine.connect() as conn:
        await conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
    await admin_engine.dispose()


def _migrate(iso_url: str, target: str) -> None:
    """Run an alembic upgrade with env.py's URL resolution pinned to ``iso_url``."""
    with pytest.MonkeyPatch().context() as mp:
        mp.setenv("DATABASE_URL", iso_url)
        mp.setenv("DATABASE_ADMIN_URL", iso_url)
        _alembic_cmd(iso_url, "upgrade", target)


def _downgrade(iso_url: str, target: str) -> None:
    with pytest.MonkeyPatch().context() as mp:
        mp.setenv("DATABASE_URL", iso_url)
        mp.setenv("DATABASE_ADMIN_URL", iso_url)
        _alembic_cmd(iso_url, "downgrade", target)


@pytest_asyncio.fixture
async def pre_upgrade_db_url(db_url: str) -> AsyncGenerator[str, None]:
    """A private Postgres database migrated to ``PREV_REV`` — the deployed
    shape where the global thread unique still exists."""
    db_name, iso_url = await _fresh_db(db_url, "m0301_pre")
    try:
        _migrate(iso_url, PREV_REV)
        yield iso_url
    finally:
        await _drop_db(db_url, db_name)


async def _engine(iso_url: str) -> AsyncEngine:
    return create_async_engine(iso_url, poolclass=NullPool)


async def _constraint_names(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT conname FROM pg_constraint WHERE conname = :name"),
            {"name": _THREAD_CONSTRAINT},
        )
        return {str(r[0]) for r in rows.all()}


async def _column_names(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'runs' "
                "AND column_name IN ('run_group_id', 'run_group_kind')"
            )
        )
        return {str(r[0]) for r in rows.all()}


async def _indexdefs(engine: AsyncEngine) -> dict[str, str]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT indexname, indexdef FROM pg_indexes "
                "WHERE schemaname = 'public' AND tablename = 'runs' AND indexname = ANY(:names)"
            ),
            {"names": list(_NEW_INDEXES)},
        )
        return {str(name): str(indexdef) for name, indexdef in rows.all()}


async def _alembic_version(engine: AsyncEngine) -> str:
    async with engine.connect() as conn:
        return str((await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one())


async def _seed(engine: AsyncEngine) -> dict[str, uuid.UUID]:
    """Seed account/org/pipeline/snapshot + three runs at ``PREV_REV`` —
    the pre-migration shape (no run_group columns exist yet). Mirrors the
    0215 harness's raw-SQL inserts."""
    ids = {
        "account": uuid.uuid4(),
        "org": uuid.uuid4(),
        "pipeline": uuid.uuid4(),
        "snapshot": uuid.uuid4(),
        "root": uuid.uuid4(),
        "child": uuid.uuid4(),
        "other": uuid.uuid4(),
    }
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO accounts (id, email, display_name, password_hash, auth_provider, active) "
                "VALUES (:id, 'f3m@example.com', 'f3m', 'hash', 'local', true)"
            ),
            {"id": str(ids["account"])},
        )
        await conn.execute(
            text("INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, 'f3m', 'f3m', '{}'::json)"),
            {"id": str(ids["org"])},
        )
        await conn.execute(
            text(
                "INSERT INTO pipelines (id, organisation_id, account_id, name, "
                "max_concurrent_runs, lock_wait_timeout_seconds, node_timeout_seconds, "
                "run_context_defaults, graph_nodes_json) "
                "VALUES (:id, :oid, :aid, 'f3m pipeline', 10, 30, 300, '{}'::json, '[]'::json)"
            ),
            {"id": str(ids["pipeline"]), "oid": str(ids["org"]), "aid": str(ids["account"])},
        )
        await conn.execute(
            text(
                "INSERT INTO pipeline_snapshots (id, pipeline_id, organisation_id, "
                "snapshot_version, graph_json, connector_bindings_json, schema_pins_json, "
                "prompt_pins_json, model_backend_pins_json, run_context_defaults, config_json) "
                "VALUES (:id, :pid, :oid, 1, '{}'::json, '[]'::json, '[]'::json, '[]'::json, "
                "'[]'::json, '{}'::json, '{}'::json)"
            ),
            {"id": str(ids["snapshot"]), "pid": str(ids["pipeline"]), "oid": str(ids["org"])},
        )
        # Three pre-existing rows: a root, its child (parent link), and an
        # unrelated run — all with DISTINCT threads, so the partial unique
        # that replaces the global one builds over them without conflict.
        for key, idx, parent in (("root", 1, None), ("child", 2, "root"), ("other", 3, None)):
            await conn.execute(
                text(
                    "INSERT INTO runs (id, organisation_id, pipeline_id, snapshot_id, trigger_type, "
                    "status, run_number, input_hash, langgraph_thread_id, parent_run_id) "
                    "VALUES (:id, :oid, :pid, :sid, 'manual', 'pending', :n, 'ih', :thread, :parent)"
                ),
                {
                    "id": str(ids[key]),
                    "oid": str(ids["org"]),
                    "pid": str(ids["pipeline"]),
                    "sid": str(ids["snapshot"]),
                    "n": idx,
                    "thread": f"seed-{key}-{ids[key].hex[:8]}",
                    "parent": str(ids[parent]) if parent else None,
                },
            )
    return ids


async def _insert_run(
    engine: AsyncEngine,
    run_id: uuid.UUID,
    ids: dict[str, uuid.UUID],
    run_number: int,
    thread: str,
    *,
    parent: uuid.UUID | None = None,
    group: uuid.UUID | None = None,
    kind: str | None = None,
) -> None:
    """One behaviour INSERT on the migrated schema (run_group columns exist)."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO runs (id, organisation_id, pipeline_id, snapshot_id, trigger_type, "
                "status, run_number, input_hash, langgraph_thread_id, "
                "parent_run_id, run_group_id, run_group_kind) "
                "VALUES (:id, :oid, :pid, :sid, 'manual', 'pending', :n, 'ih', :thread, "
                ":parent, :group, :kind)"
            ),
            {
                "id": str(run_id),
                "oid": str(ids["org"]),
                "pid": str(ids["pipeline"]),
                "sid": str(ids["snapshot"]),
                "n": run_number,
                "thread": thread,
                "parent": str(parent) if parent else None,
                "group": str(group) if group else None,
                "kind": kind,
            },
        )


async def _expect_unique_violation(
    engine: AsyncEngine, run_id: uuid.UUID, ids: dict[str, uuid.UUID], run_number: int, **kwargs: object
) -> None:
    with pytest.raises(IntegrityError) as exc_info:
        await _insert_run(engine, run_id, ids, run_number, **kwargs)  # type: ignore[arg-type]
    assert sqlstate_of(exc_info.value) == "23505", (
        f"expected SQLSTATE 23505 (unique violation), got {sqlstate_of(exc_info.value)}"
    )


# ---------------------------------------------------------------------------
# Upgrade over a populated database
# ---------------------------------------------------------------------------


async def test_negative_control_global_thread_unique_exists_before_upgrade(pre_upgrade_db_url: str) -> None:
    engine = await _engine(pre_upgrade_db_url)
    try:
        assert await _constraint_names(engine) == {_THREAD_CONSTRAINT}, "pre-state must carry the global unique"
        assert not await _column_names(engine), "run_group columns must not exist before 0301"
    finally:
        await engine.dispose()


async def test_upgrade_applies_over_existing_rows(pre_upgrade_db_url: str) -> None:
    """The migration applies over an already-populated ``runs`` table: both
    columns land, the global unique falls, both partial uniques build (the
    seeded threads are already globally unique, so the index build cannot
    fail), and the seeded rows keep their exact threads and parent link."""
    engine = await _engine(pre_upgrade_db_url)
    try:
        ids = await _seed(engine)
        seeded_threads = [f"seed-root-{ids['root'].hex[:8]}", f"seed-child-{ids['child'].hex[:8]}"]
        seeded_threads.append(f"seed-other-{ids['other'].hex[:8]}")

        _migrate(pre_upgrade_db_url, MIGRATION_REV)

        assert not await _constraint_names(engine), "global thread unique must be dropped"
        assert await _column_names(engine) == {"run_group_id", "run_group_kind"}

        async with engine.connect() as conn:
            col_rows = await conn.execute(
                text(
                    "SELECT column_name, is_nullable, column_default FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = 'runs' "
                    "AND column_name IN ('run_group_id', 'run_group_kind')"
                )
            )
            cols = {str(row[0]): (str(row[1]), row[2]) for row in col_rows.all()}
        assert cols["run_group_id"] == ("YES", None), cols
        assert cols["run_group_kind"] == ("YES", None), cols

        indexdefs = await _indexdefs(engine)
        assert set(indexdefs) == set(_NEW_INDEXES), f"expected the three new indexes, got {sorted(indexdefs)}"
        resume_def = indexdefs["uq_runs_resume_parent"]
        assert "parent_run_id" in resume_def, resume_def
        assert "WHERE" in resume_def, resume_def
        assert "'resume'" in resume_def, resume_def
        thread_def = indexdefs["uq_runs_thread_non_resume"]
        assert "langgraph_thread_id" in thread_def, thread_def
        assert "IS DISTINCT FROM" in thread_def, thread_def
        assert "'resume'" in thread_def, thread_def

        async with engine.connect() as conn:
            seeded_rows = await conn.execute(
                text(
                    "SELECT langgraph_thread_id, parent_run_id FROM runs "
                    "WHERE id IN (:root, :child, :other) ORDER BY run_number"
                ),
                {"root": str(ids["root"]), "child": str(ids["child"]), "other": str(ids["other"])},
            )
            rows = seeded_rows.all()
        assert [str(r[0]) for r in rows] == seeded_threads, "seeded threads must survive the upgrade"
        assert rows[1][1] is not None, "child row must keep its parent link"
        assert str(rows[1][1]) == str(ids["root"]), "parent link must survive"
        assert await _alembic_version(engine) == MIGRATION_REV
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Behaviour enforced by the new partial uniques on real Postgres
# ---------------------------------------------------------------------------


async def test_partial_unique_enforcement_on_postgres(pre_upgrade_db_url: str) -> None:
    engine = await _engine(pre_upgrade_db_url)
    try:
        ids = await _seed(engine)
        _migrate(pre_upgrade_db_url, MIGRATION_REV)
        root_thread = f"seed-root-{ids['root'].hex[:8]}"

        # retry: child row on a FRESH thread — accepted.
        retry_id = uuid.uuid4()
        await _insert_run(
            engine, retry_id, ids, 10, f"retry-{retry_id.hex[:8]}", parent=ids["root"], group=ids["root"], kind="retry"
        )

        # resume: child row INHERITING the parent's thread — accepted (this
        # is what the global unique used to make impossible).
        first_resume = uuid.uuid4()
        await _insert_run(
            engine, first_resume, ids, 11, root_thread, parent=ids["root"], group=ids["root"], kind="resume"
        )

        # double-resume of the same parent -> unique violation (23505).
        await _expect_unique_violation(
            engine, uuid.uuid4(), ids, 12, thread=root_thread, parent=ids["root"], group=ids["root"], kind="resume"
        )

        # resume-of-resume: its parent is a DIFFERENT row -> accepted, and
        # the whole chain keeps sharing the root's thread.
        second_resume = uuid.uuid4()
        await _insert_run(
            engine, second_resume, ids, 13, root_thread, parent=first_resume, group=ids["root"], kind="resume"
        )

        # a NON-resume row may never share an existing thread.
        await _expect_unique_violation(engine, uuid.uuid4(), ids, 14, thread=root_thread)

        # The accepted resume rows really do share the root's thread.
        async with engine.connect() as conn:
            count = (
                await conn.execute(
                    text("SELECT count(*) FROM runs WHERE langgraph_thread_id = :thread"),
                    {"thread": root_thread},
                )
            ).scalar_one()
        assert int(count) == 3, f"root + two resume rows must share one thread, got {count}"
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Downgrade refusal, recovery, and round trip
# ---------------------------------------------------------------------------


async def test_downgrade_refuses_over_shared_threads_then_round_trips(pre_upgrade_db_url: str) -> None:
    engine = await _engine(pre_upgrade_db_url)
    try:
        ids = await _seed(engine)
        _migrate(pre_upgrade_db_url, MIGRATION_REV)
        root_thread = f"seed-root-{ids['root'].hex[:8]}"
        await _insert_run(
            engine, uuid.uuid4(), ids, 10, root_thread, parent=ids["root"], group=ids["root"], kind="resume"
        )

        # Refusal: a resume child still shares its parent's thread — the
        # downgrade must NOT emit a cryptic unique violation and must NOT
        # rewrite or drop run history (the 0151/0298 precedent).
        with pytest.raises(RuntimeError, match="shared by multiple runs"):
            _downgrade(pre_upgrade_db_url, PREV_REV)
        # The refusal rolled back — we are still at the head, data intact.
        assert await _alembic_version(engine) == MIGRATION_REV
        assert await _column_names(engine) == {"run_group_id", "run_group_kind"}

        # Resolve the sharing rows, then the downgrade restores the old shape.
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM runs WHERE run_group_kind = 'resume'"))
        _downgrade(pre_upgrade_db_url, PREV_REV)

        assert await _alembic_version(engine) == PREV_REV
        assert await _constraint_names(engine) == {_THREAD_CONSTRAINT}, "downgrade must restore the global unique"
        assert not await _column_names(engine)

        async with engine.connect() as conn:
            remaining = {
                str(name)
                for name in (
                    await conn.execute(
                        text(
                            "SELECT indexname FROM pg_indexes WHERE schemaname = 'public' AND tablename = 'runs' "
                            "AND indexname = ANY(:names)"
                        ),
                        {"names": list(_NEW_INDEXES)},
                    )
                ).scalars()
            }
        assert not remaining, f"downgrade must drop the new indexes, got {sorted(remaining)}"

        # Round trip: the upgrade is re-runnable after the downgrade.
        _migrate(pre_upgrade_db_url, MIGRATION_REV)
        assert await _alembic_version(engine) == MIGRATION_REV
        assert not await _constraint_names(engine), "upgrade must drop the global unique again"
        assert await _column_names(engine) == {"run_group_id", "run_group_kind"}
        assert set(await _indexdefs(engine)) == set(_NEW_INDEXES)
    finally:
        await engine.dispose()
