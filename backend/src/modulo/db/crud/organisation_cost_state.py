"""CRUD for the per-organisation cost-state row (FAR-1833).

Plain data access only: no advisory locking and no import of ``modulo.core``.
The per-org advisory lock that serialises ceiling writes against accrual lives
in ``modulo.core.runner_capacity.lock_org_cost_state``; a caller holds that lock
and then uses these functions inside the same transaction.
"""

import uuid
from collections.abc import Mapping

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.organisation_cost_state import OrganisationCostState

__all__ = [
    "get_org_cost_state",
    "increment_org_cumulative_spend",
    "upsert_org_cost_state",
]

# The only columns an upsert may name. A caller naming anything else has a bug
# (a typo, or a column this module does not own), so refuse rather than write.
_ALLOWED_UPSERT_COLUMNS: frozenset[str] = frozenset(
    {"max_run_cost_cents", "spend_ceiling_cents", "org_cumulative_spend_cents"}
)


async def get_org_cost_state(session: AsyncSession, org_id: uuid.UUID) -> OrganisationCostState | None:
    """Return the org's cost-state row, or ``None`` when no row exists yet.

    ``None`` is the defaults contract (both ceilings unlimited, cumulative 0),
    not an error.
    """
    result = await session.execute(select(OrganisationCostState).where(OrganisationCostState.org_id == org_id))
    return result.scalar_one_or_none()


async def upsert_org_cost_state(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    provided: Mapping[str, int | None],
) -> None:
    """Insert or patch the org's cost-state row, writing ONLY the named columns.

    ``provided`` maps a column name (one of ``max_run_cost_cents``,
    ``spend_ceiling_cents``, ``org_cumulative_spend_cents``) to its new value;
    an explicit ``None`` clears a nullable ceiling. The ``ON CONFLICT DO UPDATE``
    sets only the named columns, so a ceiling patch never clobbers the
    cumulative spend and an accrual write never clobbers a ceiling. On a first
    write for an org the INSERT creates the row with the named columns, and the
    unnamed ones take their column defaults (cumulative 0).

    Raises ``ValueError`` for an empty mapping or an unknown column name — a
    silent no-op or an accidental extra column would both hide a caller bug.
    """
    unknown = sorted(set(provided) - _ALLOWED_UPSERT_COLUMNS)
    if unknown:
        raise ValueError(f"upsert_org_cost_state got unknown column(s): {unknown}")
    if not provided:
        raise ValueError("upsert_org_cost_state requires at least one column to write")

    insert_stmt = pg_insert(OrganisationCostState).values(org_id=org_id, **provided)
    update_set = {column: getattr(insert_stmt.excluded, column) for column in provided}
    await session.execute(insert_stmt.on_conflict_do_update(index_elements=["org_id"], set_=update_set))


async def increment_org_cumulative_spend(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    delta_cents: int,
) -> int:
    """Atomically add ``delta_cents`` to the org's cumulative spend and return the total.

    A SINGLE statement — never a Python read-modify-write — so two concurrent
    callers cannot lose an update: the addition is computed by the database in
    the same statement that reads the old value. A first call for an org with no
    row inserts it with the delta as the starting total. A delta that would drive
    the total below zero is refused by the ``ck_organisation_cost_state_cum_spend``
    CHECK constraint.
    """
    insert_stmt = pg_insert(OrganisationCostState).values(org_id=org_id, org_cumulative_spend_cents=delta_cents)
    stmt = insert_stmt.on_conflict_do_update(
        index_elements=["org_id"],
        set_={"org_cumulative_spend_cents": OrganisationCostState.org_cumulative_spend_cents + delta_cents},
    ).returning(OrganisationCostState.org_cumulative_spend_cents)
    result = await session.execute(stmt)
    return int(result.scalar_one())
