"""FAR-1665: historical ``run_daily_facts.benign`` repair (migration 0298), end to end.

FAR-707's live path stamps ``benign`` at finalize and the reconcile sweep
re-records the fact when it lands a benign verdict — but facts written BEFORE
that fix keep ``benign=NULL`` next to a run the sweep later classified
``benign``: ``backfill_facts``' anti-join only inserts runs with NO fact, and
the sweep skips already-classified runs, so nothing repairs them. Readers
treat NULL as "not benign", so those historical benign outcomes keep inflating
analytics failure rates.

This module drives the REAL alembic upgrade of ``0298_benign_facts_repair``
against a private Postgres database (testcontainers) seeded with the four
historical fact shapes:

* benign classification + ``benign=NULL`` fact  -> repaired to ``TRUE``;
* non-benign (``no_delivery``) classification + ``benign=NULL`` fact -> stays
  ``NULL``;
* benign classification + confidently ``FALSE`` fact -> stays ``FALSE`` (the
  repair must never overwrite a confident live-writer verdict);
* no classification at all + ``benign=NULL`` fact -> stays ``NULL``.

plus the negative control (all four are in their pre-repair state at
``0297``) and the idempotency proof: ``downgrade`` is a data no-op and a
second ``upgrade`` leaves every row byte-identical.

The private-database fixture mirrors ``test_migration_0275_run_cancel_reason_vocabulary.py``;
seeders are shared with ``test_org_sandbox_capacity``.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from tests.integration.test_org_sandbox_capacity import (
    _SANDBOX_GRAPH,
    _seed_org_account,
    _seed_pipeline,
    _seed_run,
    _seed_snapshot,
)

pytestmark = [pytest.mark.integration]

BACKEND_ROOT = Path(__file__).parents[2]  # backend/
MIGRATION_REV = "0298_benign_facts_repair"
PREV_REV = "0297_autonomy_gating_s1"


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


@pytest_asyncio.fixture
async def isolated_db_url(db_url: str, monkeypatch: pytest.MonkeyPatch):
    """A fresh, private Postgres database migrated only up to ``PREV_REV``.

    ``env.py`` resolves the target DB from ``DATABASE_URL`` /
    ``DATABASE_ADMIN_URL``, so both are pinned for every alembic call."""
    admin_engine = create_async_engine(db_url, poolclass=NullPool, execution_options={"isolation_level": "AUTOCOMMIT"})
    db_name = f"m0298_iso_{uuid.uuid4().hex[:10]}"
    async with admin_engine.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{db_name}" WITH TEMPLATE template0'))
    await admin_engine.dispose()

    iso_url = _swap_db_name(db_url, db_name)
    engine = create_async_engine(iso_url, poolclass=NullPool)
    async with engine.connect() as conn:
        await conn.execute(
            text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(255) NOT NULL PRIMARY KEY)")
        )
    await engine.dispose()

    monkeypatch.setenv("DATABASE_URL", iso_url)
    monkeypatch.setenv("DATABASE_ADMIN_URL", iso_url)
    with pytest.MonkeyPatch().context() as mp:
        mp.setenv("DATABASE_URL", iso_url)
        mp.setenv("DATABASE_ADMIN_URL", iso_url)
        _alembic_cmd(iso_url, "upgrade", PREV_REV)

    yield iso_url

    admin_engine = create_async_engine(db_url, poolclass=NullPool, execution_options={"isolation_level": "AUTOCOMMIT"})
    async with admin_engine.connect() as conn:
        await conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
    await admin_engine.dispose()


async def _alembic_upgrade(iso_url: str, target: str) -> None:
    """Apply the REAL alembic upgrade of *target* against the isolated DB."""
    with pytest.MonkeyPatch().context() as mp:
        mp.setenv("DATABASE_URL", iso_url)
        mp.setenv("DATABASE_ADMIN_URL", iso_url)
        _alembic_cmd(iso_url, "upgrade", target)


async def _alembic_downgrade(iso_url: str, target: str) -> None:
    with pytest.MonkeyPatch().context() as mp:
        mp.setenv("DATABASE_URL", iso_url)
        mp.setenv("DATABASE_ADMIN_URL", iso_url)
        _alembic_cmd(iso_url, "downgrade", target)


async def _set_classification(engine: AsyncEngine, run_id: uuid.UUID, value: str | None) -> None:
    """Persist (or clear) the run's terminal classification record."""
    async with engine.begin() as conn:
        if value is None:
            await conn.execute(text("UPDATE runs SET run_classification = NULL WHERE id = :rid"), {"rid": str(run_id)})
        else:
            record = json.dumps({"value": value, "reason": "test", "computed_at": "2026-10-11T00:00:00Z"})
            await conn.execute(
                text("UPDATE runs SET run_classification = CAST(:rc AS jsonb) WHERE id = :rid"),
                {"rc": record, "rid": str(run_id)},
            )


async def _seed_fact(engine: AsyncEngine, org_id: uuid.UUID, run_id: uuid.UUID, *, benign: bool | None) -> None:
    """A historical-shaped fact row (raw SQL so ``benign`` can be NULL)."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO run_daily_facts "
                "(id, organisation_id, run_id, run_date, trigger_type, status, benign) "
                "VALUES (:id, :oid, :rid, CURRENT_DATE, 'manual', 'failed', :benign)"
            ),
            {"id": str(uuid.uuid4()), "oid": str(org_id), "rid": str(run_id), "benign": benign},
        )


async def _fact_benign(engine: AsyncEngine, run_id: uuid.UUID) -> bool | None:
    async with engine.connect() as conn:
        row = await conn.execute(text("SELECT benign FROM run_daily_facts WHERE run_id = :rid"), {"rid": str(run_id)})
        return row.scalar_one()


async def _alembic_version(engine: AsyncEngine) -> str:
    async with engine.connect() as conn:
        return str((await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one())


async def _seed_historical_rows(engine: AsyncEngine) -> dict[str, uuid.UUID]:
    """Org + pipeline + snapshot + the four historical fact shapes.

    Returns a label->run_id map. Each run is terminal (``failed``) with the
    classification the reconcile sweep would have persisted, beside a fact row
    carrying the historical ``benign`` marker (or confident lack of one).
    """
    org_id, user_id = await _seed_org_account(engine, "F1665RepairOrg", cap=None)
    pipeline_id = await _seed_pipeline(engine, org_id, "F1665Pipe", user_id)
    snapshot_id = await _seed_snapshot(engine, org_id, pipeline_id, _SANDBOX_GRAPH)

    runs: dict[str, uuid.UUID] = {}
    shapes: list[tuple[str, str | None, bool | None]] = [
        # (label, classification value, fact benign marker)
        ("null_beside_benign", "benign", None),  # (a) the repair target
        ("null_beside_no_delivery", "no_delivery", None),  # (b) non-benign stays NULL
        ("false_beside_benign", "benign", False),  # (b) confident FALSE never overwritten
        ("null_beside_unclassified", None, None),  # (b) no classification stays NULL
    ]
    for label, classification, benign in shapes:
        run_id = await _seed_run(engine, org_id, pipeline_id, snapshot_id, status="failed")
        await _set_classification(engine, run_id, classification)
        await _seed_fact(engine, org_id, run_id, benign=benign)
        runs[label] = run_id
    return runs


async def _assert_historical_state(engine: AsyncEngine, runs: dict[str, uuid.UUID]) -> None:
    """The PRE-repair state every shape must sit in at ``0297``."""
    assert await _fact_benign(engine, runs["null_beside_benign"]) is None
    assert await _fact_benign(engine, runs["null_beside_no_delivery"]) is None
    assert await _fact_benign(engine, runs["false_beside_benign"]) is False
    assert await _fact_benign(engine, runs["null_beside_unclassified"]) is None


async def _assert_repaired_state(engine: AsyncEngine, runs: dict[str, uuid.UUID]) -> None:
    """The POST-repair state: only the NULL-next-to-benign row flipped."""
    assert await _fact_benign(engine, runs["null_beside_benign"]) is True
    assert await _fact_benign(engine, runs["null_beside_no_delivery"]) is None
    assert await _fact_benign(engine, runs["false_beside_benign"]) is False
    assert await _fact_benign(engine, runs["null_beside_unclassified"]) is None


class TestBenignFactsRepair:
    async def test_repair_flips_only_null_next_to_benign_and_is_idempotent(self, isolated_db_url: str) -> None:
        """0298 stamps ``benign=TRUE`` for NULL facts whose run classed
        benign, leaves every other shape untouched, and re-running is a no-op.

        Without 0298 this test FAILS at step 2: the chain head stays at 0297
        and ``null_beside_benign`` remains NULL — the historical inflation
        reproduced verbatim.
        """
        engine = create_async_engine(isolated_db_url, poolclass=NullPool)
        try:
            runs = await _seed_historical_rows(engine)

            # 1. Negative control: at 0297 the repair has not run, so the
            #    historical defect state is in place.
            assert await _alembic_version(engine) == PREV_REV
            await _assert_historical_state(engine, runs)

            # 2. The REAL migration repairs exactly the NULL-next-to-benign row.
            await _alembic_upgrade(isolated_db_url, MIGRATION_REV)
            assert await _alembic_version(engine) == MIGRATION_REV
            await _assert_repaired_state(engine, runs)

            # 3. Idempotency: downgrade is a data no-op, and a second upgrade
            #    re-runs the repair against an already-repaired table without
            #    changing any row.
            await _alembic_downgrade(isolated_db_url, PREV_REV)
            assert await _alembic_version(engine) == PREV_REV
            await _assert_repaired_state(engine, runs)

            await _alembic_upgrade(isolated_db_url, MIGRATION_REV)
            assert await _alembic_version(engine) == MIGRATION_REV
            await _assert_repaired_state(engine, runs)
        finally:
            await engine.dispose()
