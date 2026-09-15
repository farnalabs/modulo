"""Reflection contract for the Organisation ORM model (FAR-872 regression).

The FAR-872 incident was an ORM-vs-DB mismatch: the ORM declared
``updated_at`` but ``organisations.updated_at`` had been dropped from Postgres
by migration 0243_remove_organisations_audit_drift (0239 was a no-op), so every
``select(Organisation)`` raised
``ProgrammingError: column organisations.updated_at does not exist``.

A unit test cannot catch that drift — ``sa_inspect(Organisation).columns`` and
``Organisation.__table__.columns`` are the same set by construction, so
comparing them is a tautology. Detecting it requires reflecting the migrated
schema from a live Postgres, which is what this integration test does. The
ORM-side guards live in ``tests/unit/db/test_organisation_model_contract.py``.
"""

import pytest
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncEngine

from modulo.db.models.organisation import Organisation

pytestmark = pytest.mark.integration

# Audit columns dropped from ``organisations`` by migration 0243. The migrated
# table must no longer carry them.
_REMOVED_AUDIT_COLUMNS = frozenset({"updated_at", "updated_by", "deleted_by"})


async def _reflected_columns(engine: AsyncEngine, table: str) -> set[str]:
    async with engine.connect() as connection:
        return await connection.run_sync(
            lambda sync_connection: {column["name"] for column in inspect(sync_connection).get_columns(table)}
        )


async def test_organisation_orm_columns_exist_in_migrated_schema(db_engine: AsyncEngine) -> None:
    """Every ORM-mapped Organisation column must exist in the migrated table.

    This is the FAR-872 regression guard: an ORM column with no matching DB
    column only surfaces as an ``UndefinedColumnError`` at query time on live
    data, so it must be caught by reflecting the real schema here.
    """
    orm_columns = {column.name for column in Organisation.__table__.columns}
    db_columns = await _reflected_columns(db_engine, "organisations")

    missing_in_db = orm_columns - db_columns
    assert not missing_in_db, (
        f"ORM declares Organisation columns absent from the migrated schema: {sorted(missing_in_db)}. "
        f"ORM columns: {sorted(orm_columns)}; DB columns: {sorted(db_columns)}"
    )


async def test_organisation_audit_columns_absent_from_migrated_schema(db_engine: AsyncEngine) -> None:
    """The audit columns dropped on main must be absent DB-side (FAR-872)."""
    db_columns = await _reflected_columns(db_engine, "organisations")

    still_present = _REMOVED_AUDIT_COLUMNS & db_columns
    assert not still_present, f"organisations still carries dropped audit columns: {sorted(still_present)}"
