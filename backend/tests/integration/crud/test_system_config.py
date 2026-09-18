"""Integration tests for SystemConfig CRUD against a real database.

These exercise the ``set_config`` first-write-wins convergence against actual
concurrent connections (the mock-shaded unit test in ``tests/unit/db`` only
covers control flow). The TOFU correctness guarantee in FAR-359 depends on the
losing caller converging to the winner's stored value rather than re-emitting
its own pending INSERT.
"""

import asyncio
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from modulo.db.crud.system_config import set_config, update_config
from modulo.db.models.system_config import SystemConfig

pytestmark = pytest.mark.integration


async def _clean_key(db_engine, key: str) -> None:
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(text("DELETE FROM system_config WHERE key = :k"), {"k": key})


async def test_set_config_concurrent_first_write_converges(db_engine) -> None:
    """Two concurrent first-writes of the same key converge to ONE row.

    Both sessions open their own transaction and race the unique ``key`` INSERT.
    One wins and commits; the loser's ``set_config`` must roll back to the
    savepoint, **expunge its still-pending entity** (otherwise the trailing
    flush re-emits the conflicting INSERT and raises an uncaught IntegrityError),
    re-select the winner's row, and adopt it. Both callers must observe the same
    single stored value — the first-write-wins / TOFU invariant.
    """
    key = f"tofu_convergence_{uuid.uuid4().hex}"
    await _clean_key(db_engine, key)

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    winner_value = {"secret": "winner-secret"}
    loser_value = {"secret": "loser-secret"}

    async def mint(value):
        async with factory() as s:
            result = await set_config(s, key, value)
            await s.commit()
            return result

    r1, r2 = await asyncio.gather(mint(winner_value), mint(loser_value))

    # Exactly one row exists for the key.
    async with factory() as s3:
        rows = (await s3.execute(select(SystemConfig).where(SystemConfig.key == key))).scalars().all()

    assert len(rows) == 1
    stored = rows[0].value

    # Both callers observe the SAME stored value (the winner's).
    assert r1.value == stored
    assert r2.value == stored
    assert r1.value == r2.value
    # The stored value is whichever caller won the race — never a torn mix.
    assert stored in (winner_value, loser_value)


async def test_set_config_existing_row_updates_in_place(db_engine) -> None:
    """A deliberate update overwrites — via ``update_config`` (last-writer-wins)."""
    key = f"tofu_update_{uuid.uuid4().hex}"
    await _clean_key(db_engine, key)

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as s:
        first = await update_config(s, key, {"v": 1})
        await s.commit()
        assert first.value == {"v": 1}

    async with factory() as s:
        second = await update_config(s, key, {"v": 2})
        await s.commit()
        assert second.value == {"v": 2}

    async with factory() as s:
        rows = (await s.execute(select(SystemConfig).where(SystemConfig.key == key))).scalars().all()
    assert len(rows) == 1
    assert rows[0].value == {"v": 2}


async def test_set_config_second_write_converges_to_first_committed(db_engine) -> None:
    """Deterministic TOFU check: a second ``set_config`` adopts the first value.

    The reviewer's reproduction — winner ``set_config``s and commits fully, then
    a second writer runs ``set_config`` on the same key — must converge to the
    winner's stored value rather than overwriting it. ``set_config`` is
    unconditional-TOFU, so this holds deterministically for every interleaving,
    not only for the racing case.
    """
    key = f"tofu_sequential_{uuid.uuid4().hex}"
    await _clean_key(db_engine, key)

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as s:
        first = await set_config(s, key, {"secret": "first-writer"})
        await s.commit()
        assert first.value == {"secret": "first-writer"}

    async with factory() as s:
        second = await set_config(s, key, {"secret": "second-writer"})
        await s.commit()
        # Second writer adopts the first writer's committed value — no overwrite.
        assert second.value == {"secret": "first-writer"}

    async with factory() as s:
        rows = (await s.execute(select(SystemConfig).where(SystemConfig.key == key))).scalars().all()
    assert len(rows) == 1
    assert rows[0].value == {"secret": "first-writer"}
