"""FAR-2040 EXPAND release: the application layer requires ``eval_results.outcome``.

Migration 0325 cannot set NOT NULL while the previous image is still serving
(that image has no ``outcome`` column and would fail every insert). The
``before_insert`` guard on ``EvalResult`` is the contract in the meantime, so a
new-image write that forgets the value fails loudly instead of introducing a
NULL the DATABASE still permits.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from modulo.db.models import Base
from modulo.db.models.eval_result import EvalResult


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=[EvalResult.__table__])
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


def _result(**kwargs):
    values = {
        "organisation_id": uuid.uuid4(),
        "run_id": uuid.uuid4(),
        "eval_id": uuid.uuid4(),
        "passed": True,
    }
    values.update(kwargs)
    return EvalResult(**values)


async def test_a_write_without_outcome_fails_loudly(session) -> None:
    session.add(_result())

    with pytest.raises(ValueError, match="outcome is required"):
        await session.flush()


async def test_a_write_with_outcome_succeeds(session) -> None:
    row = _result(outcome="pass")
    session.add(row)
    await session.flush()

    assert row.outcome == "pass"
