"""Unit tests for migration 0299_accounts_service_identity (FAR-1385 m-2).

Two layers:

* the migration wiring — revision chain, the CHECK it creates, its pre-flight
  guard against an unsatisfiable row, and its downgrade; and
* real enforcement — the CHECK itself, exercised against an in-memory SQLite
  ``accounts`` table (the constraint is dialect-independent), proving a service
  row cannot carry an email, password hash or SSO subject while a human row
  and a NULL-identity service row are both accepted.
"""

from __future__ import annotations

import importlib.util
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import StaticPool, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.db.models import Account, Base
from modulo.db.models.account import KIND_HUMAN, KIND_SERVICE

_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "modulo"
    / "db"
    / "migrations"
    / "versions"
    / "0299_accounts_service_identity.py"
)
_CHECK = "ck_accounts_service_identity"


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0299", _PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_chains_off_0298_accounts_kind() -> None:
    module = _load_migration()
    assert module.revision == "0299_accounts_service_identity"
    assert module.down_revision == "0298_accounts_kind"


def test_upgrade_creates_the_identity_check() -> None:
    module = _load_migration()
    with patch.object(module, "op") as op:
        op.get_bind.return_value.execute.return_value.scalar.return_value = 0
        module.upgrade()
    op.create_check_constraint.assert_called_once_with(
        _CHECK,
        "accounts",
        "kind = 'human' OR (email IS NULL AND password_hash IS NULL AND sso_subject IS NULL)",
    )


def test_upgrade_refuses_when_a_service_row_carries_human_identity() -> None:
    module = _load_migration()
    with patch.object(module, "op") as op:
        op.get_bind.return_value.execute.return_value.scalar.return_value = 2
        with pytest.raises(RuntimeError, match="service account row"):
            module.upgrade()
    op.create_check_constraint.assert_not_called()


def test_downgrade_drops_the_identity_check() -> None:
    module = _load_migration()
    with patch.object(module, "op") as op:
        module.downgrade()
    op.drop_constraint.assert_called_once_with(_CHECK, "accounts", type_="check")


@pytest.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine: AsyncEngine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=[Account.__table__]))
    maker = async_sessionmaker(engine, expire_on_commit=False, autobegin=False)
    async with maker() as s:
        yield s
    await engine.dispose()


async def _insert(session: AsyncSession, **columns: Any) -> uuid.UUID:
    row_id = uuid.uuid4()
    row = Account(id=row_id, display_name="row", **columns)
    async with session.begin():
        session.add(row)
        await session.flush()
    return row_id


async def _load(session: AsyncSession, row_id: uuid.UUID) -> Account:
    async with session.begin():
        return (await session.execute(select(Account).where(Account.id == row_id))).scalar_one()


async def test_human_row_with_identity_is_accepted(session: AsyncSession) -> None:
    row_id = await _insert(session, kind=KIND_HUMAN, email="human@example.com", password_hash="hash")
    stored = await _load(session, row_id)
    assert stored.kind == KIND_HUMAN
    assert stored.email == "human@example.com"


async def test_service_row_with_no_identity_is_accepted(session: AsyncSession) -> None:
    row_id = await _insert(session, kind=KIND_SERVICE, email=None, password_hash=None, sso_subject=None)
    stored = await _load(session, row_id)
    assert stored.kind == KIND_SERVICE
    assert stored.email is None


@pytest.mark.parametrize("column", ["email", "password_hash", "sso_subject"])
async def test_service_row_with_human_identity_is_rejected(session: AsyncSession, column: str) -> None:
    """The invariant is structural: the DB refuses a reachable service row."""
    with pytest.raises(IntegrityError, match=_CHECK):
        await _insert(session, kind=KIND_SERVICE, **{column: "present"})
