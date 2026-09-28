"""0264: ``ck_pipelines_max_autonomy_ge_default`` against real Postgres.

Migration 0256/0259 guarded the VOCABULARY of both autonomy columns but never
their RELATIVE ORDER - the ``ceiling >= default`` invariant lived only in the
app-layer PATCH lock. 0264 repairs inverted rows (behaviour-preserving) and
adds the composite CHECK.

Runs against the migrated testcontainer (real Postgres):

* an INVERTED pair (ceiling below default) must be rejected by the DATABASE
  - the constraint name in the error is the evidence the right CHECK fired,
* a NULL-ceiling row (and a NULL-default row) must be accepted,
* the migration's own repair statement, run against a row it can actually
  see (the CHECK dropped first, since post-migration no inverted row can be
  inserted), must lower the default onto the ceiling AND leave the effective
  autonomy resolution unchanged.
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path
from types import ModuleType

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from modulo.core.run_context.autonomy import PIPELINE_MAX_AUTONOMY_KEY, resolve_autonomy

pytestmark = pytest.mark.integration

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "modulo"
    / "db"
    / "migrations"
    / "versions"
    / "0264_pipelines_max_autonomy_ge_default.py"
)
_CONSTRAINT = "ck_pipelines_max_autonomy_ge_default"

_MANUAL = "manual_approval"
_FULL = "fully_autonomous"
_NOTIFY = "notify_on_complete"


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0264_pipelines_max_autonomy_ge_default", _MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _upgrade_statements() -> tuple[str, str, str]:
    statements = _load_migration().UPGRADE_STATEMENTS
    assert len(statements) == 3, f"expected repair + add + validate, got {len(statements)}"
    return statements[0], statements[1], statements[2]


async def _insert_pipeline(
    db_engine: AsyncEngine,
    org_id: uuid.UUID,
    account_id: uuid.UUID,
    *,
    default: str | None,
    ceiling: str | None,
) -> uuid.UUID:
    """Minimal committed pipelines row (own row - never the shared fixture).

    ``pipelines.account_id`` is a NOT NULL FK to ``accounts``, so a valid
    account is required - the session-scoped ``test_user`` fixture supplies it.
    """
    pipeline_id = uuid.uuid4()
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO pipelines (id, organisation_id, name, account_id, max_concurrent_runs, "
                "lock_wait_timeout_seconds, node_timeout_seconds, run_context_defaults, "
                "graph_nodes_json, default_autonomy_level, max_autonomy_level) "
                "VALUES (:id, :oid, :name, :aid, 10, 30, 300, '{}'::json, '[]'::json, :default, :ceiling)",
            ),
            {
                "id": str(pipeline_id),
                "oid": str(org_id),
                "aid": str(account_id),
                "name": f"ge-default-{pipeline_id.hex[:8]}",
                "default": default,
                "ceiling": ceiling,
            },
        )
    return pipeline_id


async def _cleanup(db_engine: AsyncEngine, pipeline_id: uuid.UUID) -> None:
    async with db_engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM pipeline_snapshots WHERE pipeline_id = :pid"),
            {"pid": str(pipeline_id)},
        )
        await conn.execute(
            text("DELETE FROM runs WHERE pipeline_id = :pid"),
            {"pid": str(pipeline_id)},
        )
        await conn.execute(
            text("DELETE FROM pipeline_edges WHERE pipeline_id = :pid"),
            {"pid": str(pipeline_id)},
        )
        await conn.execute(text("DELETE FROM pipelines WHERE id = :id"), {"id": str(pipeline_id)})


async def _constraint_validated(db_engine: AsyncEngine) -> bool:
    async with db_engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT convalidated FROM pg_constraint WHERE conname = :name"),
                {"name": _CONSTRAINT},
            )
        ).first()
    if row is None:
        return False
    return bool(row[0])


async def _restore_constraint(db_engine: AsyncEngine) -> None:
    """Re-add + VALIDATE 0264's CHECK (both statements are existence-gated)."""
    _repair, add_ddl, validate_ddl = _upgrade_statements()
    async with db_engine.begin() as conn:
        await conn.execute(text(add_ddl))
        await conn.execute(text(validate_ddl))


async def test_inverted_pair_is_rejected_by_the_db(
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """``ceiling < default`` fails at the DATABASE - not just at the app layer.

    The rejected INSERT persists nothing, so there is no row to clean up.
    """
    with pytest.raises(DBAPIError) as excinfo:
        await _insert_pipeline(
            db_engine,
            test_org,
            test_user,
            default=_FULL,
            ceiling=_MANUAL,
        )
    message = str(excinfo.value)
    assert _CONSTRAINT in message, message
    assert "violates check constraint" in message.lower(), message


@pytest.mark.parametrize(
    ("default", "ceiling"),
    [
        # NULL ceiling = "ceiling is the default": always valid (first arm).
        (_FULL, None),
        # NULL default: the CASE comparison is NULL, which a CHECK satisfies.
        (None, _MANUAL),
        # Equal and ordered pairs.
        (_MANUAL, _MANUAL),
        (_NOTIFY, _FULL),
    ],
)
async def test_valid_pairs_round_trip(
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
    default: str | None,
    ceiling: str | None,
) -> None:
    pipeline_id = await _insert_pipeline(
        db_engine,
        test_org,
        test_user,
        default=default,
        ceiling=ceiling,
    )
    try:
        async with db_engine.connect() as conn:
            stored = (
                await conn.execute(
                    text("SELECT default_autonomy_level, max_autonomy_level FROM pipelines WHERE id = :id"),
                    {"id": str(pipeline_id)},
                )
            ).one()
        assert stored[0] == default
        assert stored[1] == ceiling
    finally:
        await _cleanup(db_engine, pipeline_id)


async def test_repair_lowers_the_default_onto_the_ceiling_and_preserves_behaviour(
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """The migration's DATA-MUTATING statement, on a row it can actually see.

    Post-migration no inverted row can be INSERTed, so the CHECK is dropped
    first (restored in ``finally`` - both DDL statements are existence-gated,
    so the restore is idempotent and also fixes a mid-test failure).

    The repaired row must (a) satisfy the invariant and (b) resolve autonomy
    EXACTLY as before: ``resolve_autonomy`` computes
    ``base = min(default, ceiling)``, which for an inverted row is already the
    ceiling, so lowering the default onto it changes nothing observable.
    """
    repair_ddl, _add_ddl, _validate_ddl = _upgrade_statements()
    pipeline_id: uuid.UUID | None = None
    # Literal DDL (the constraint name is spelled out rather than formatted in
    # - no f-string SQL). Dropping the CHECK is what makes the inverted row
    # insertable at all; it is restored in ``finally``.
    async with db_engine.begin() as conn:
        await conn.execute(text("ALTER TABLE pipelines DROP CONSTRAINT ck_pipelines_max_autonomy_ge_default"))
    try:
        pipeline_id = await _insert_pipeline(
            db_engine,
            test_org,
            test_user,
            default=_FULL,
            ceiling=_MANUAL,
        )

        run_context = {PIPELINE_MAX_AUTONOMY_KEY: _MANUAL}
        before = resolve_autonomy(_FULL, run_context).effective
        # A context-setter recommendation is clamped by the same min() both
        # before and after the repair - check that arm too.
        recommended = dict(run_context)
        recommended["autonomy_recommendation"] = _NOTIFY
        before_with_rec = resolve_autonomy(_FULL, recommended).effective

        async with db_engine.begin() as conn:
            await conn.execute(text(repair_ddl))

        async with db_engine.connect() as conn:
            row = (
                await conn.execute(
                    text("SELECT default_autonomy_level, max_autonomy_level FROM pipelines WHERE id = :id"),
                    {"id": str(pipeline_id)},
                )
            ).one()
        assert row[0] == _MANUAL, f"repair must lower the default onto the ceiling, got {row[0]}"
        assert row[1] == _MANUAL
        # The invariant itself now holds for the repaired row.
        assert row[0] == row[1]

        after = resolve_autonomy(str(row[0]), run_context).effective
        assert after == before, f"effective autonomy changed: {before} -> {after}"
        after_with_rec = resolve_autonomy(str(row[0]), recommended).effective
        assert after_with_rec == before_with_rec, f"recommendation clamp changed: {before_with_rec} -> {after_with_rec}"
    finally:
        await _restore_constraint(db_engine)
        assert await _constraint_validated(db_engine), "the CHECK must be restored and VALIDATEd"
        if pipeline_id is not None:
            await _cleanup(db_engine, pipeline_id)
