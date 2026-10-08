"""Mutation row-lock bound — shared by route slices and core sweeps.

Idle-in-transaction lock waits are bounded at the application level with a
transaction-scoped ``lock_timeout`` (FAR-1313 / FAR-1279). The helper lives in
``db/crud`` (not in either route module) so no route needs a cross-route
import — a function-local import of a sibling route module is a semgrep
blocking finding and the cross-route edge is exactly the cycle this avoids.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from modulo.db.crud.run import get_dialect_name
from modulo.settings import get_settings


async def set_mutation_row_lock_timeout(session: AsyncSession | AsyncConnection) -> None:
    """Bound every row-lock wait taken for the REST of the current transaction.

    FAR-1313: the bound used to be set only inside
    ``_reapply_team_gate_inside_mutation_txn``, so a mutation transaction whose
    FIRST lock came from somewhere else ran that lock unbounded - e.g. the
    clone endpoint's ``check_pipeline_name_available`` (``SELECT ... FOR
    UPDATE`` on the target-name row), which fires BEFORE the clone's in-txn
    team gate (FAR-1276) reaches ``clone_pipeline``'s step-(a) commit hook.
    Endpoints whose mutation transaction takes a lock of its own call this at
    the top of the transaction, before any lock; the gate helper calls it too,
    so every mutation transaction sets the bound before its first lock.

    Transaction-scoped (``set_config(..., is_local => true)`` == ``SET LOCAL``),
    so the bound applies to every lock the transaction takes after it and
    reverts on COMMIT/ROLLBACK - never to pooled connections. It takes no lock
    itself, so calling it up front never conflicts with a later lock ordering
    (the clone's step-(a) ``FOR SHARE`` runs on a SEPARATE connection and is
    bounded by its own copy of this statement - see
    ``db.crud.pipeline._read_clone_source_snapshot``).

    POSTGRES-ONLY (the shared dialect gate in :func:`_dialect_name`, which
    reaches the same ``db.crud.run.get_dialect_name`` helper
    ``core.hitl_manager.gate_coalescing`` and ``db/rls.py`` use, rather than
    another local bind dance): SQLite has no ``set_config``,
    and the unit fixtures run on SQLite mocks. A bind that does not positively
    report ``postgresql`` skips the statement: the lock bound this sets is a
    safety improvement, never a correctness requirement, so the safe direction
    to fail is the no-op.

    The bound itself is ``Settings.mutation_row_lock_timeout_ms`` (FAR-1279):
    read at the call site so an operator can relax it without a code change.

    Accepts an ``AsyncConnection`` as well as an ``AsyncSession`` (FAR-1592):
    the periodic sweeps in ``core.run_admission`` drive their per-org
    transaction off ``engine.connect()``, not a session, and a sweep's
    multi-row ``UPDATE runs`` is exactly the kind of hot-table write this
    bound exists for. The dialect gate resolves through the connection's own
    ``dialect`` attribute for that shape — ``AsyncConnection`` does not proxy
    ``get_bind()`` (SQLAlchemy generates proxies for the listed ATTRIBUTES
    only, and ``get_bind`` is a method), while ``AsyncSession`` does.
    """
    if await _dialect_name(session) == "postgresql":
        lock_timeout_ms = get_settings().mutation_row_lock_timeout_ms
        await session.execute(
            text("SELECT set_config('lock_timeout', :val, true)"),
            {"val": f"{lock_timeout_ms}ms"},
        )


async def _dialect_name(session: AsyncSession | AsyncConnection) -> str:
    """Dialect name of *session* or *connection* (the FAR-1592 sweep seam).

    Mirrors ``db.crud.run.get_dialect_name`` for the session shape (same
    coroutine-tolerant bind dance) and reads the proxied ``dialect`` attribute
    for the connection shape, where ``get_bind()`` does not exist. The caller
    only ever asks "is this postgresql?", so both paths answer the same
    question from the same underlying ``Engine.dialect``.
    """
    if isinstance(session, AsyncConnection):
        return session.dialect.name
    return await get_dialect_name(session)
