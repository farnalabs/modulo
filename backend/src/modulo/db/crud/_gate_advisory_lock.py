"""Shared per-eval policy-gate advisory lock (FAR-1666 / FAR-1688).

The declarative graph writer (``modulo.db.crud.pipeline``) and the in-product
authoring routes (``modulo.api.routes.evals``) must serialise on the SAME
transaction-scoped advisory lock — key ``policy_gate:<eval_id>`` through
Postgres ``hashtext`` — so a graph write and a concurrent authoring write can
never race the create-or-replace sequence.

This module is the ONE derivation of that lock. It lives in the ``db`` layer
rather than ``api`` because the ``db-does-not-import-core`` import-linter
contract forbids ``modulo.db`` -> ``modulo.api``: an ``api``-owned helper could
never be the neutral home both layers may import. Both layers therefore import
from here, and a future edit to the key/timeout can only happen in one place.
"""

import uuid
from collections.abc import Awaitable, Callable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_GATE_LOCK_TIMEOUT_SQL = "SET LOCAL statement_timeout = '5s'"
_GATE_ADVISORY_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtext(:key))"
GATE_LOCK_KEY_PREFIX = "policy_gate:"


def policy_gate_lock_key(eval_id: uuid.UUID) -> str:
    """Return the canonical advisory-lock key for *eval_id*.

    The SINGLE derivation site: both the DB graph writer and the API authoring
    routes reach the lock through :func:`with_gate_advisory_lock`, never by
    building the key themselves.
    """
    return f"{GATE_LOCK_KEY_PREFIX}{eval_id}"


async def with_gate_advisory_lock[T](
    eval_id: uuid.UUID,
    session: AsyncSession,
    fn: Callable[..., Awaitable[T]],
) -> T:
    """Run *fn* while holding the transaction-scoped per-eval advisory lock.

    The lock releases when the surrounding transaction commits or rolls back.
    ``SET LOCAL statement_timeout`` bounds how long the session waits on
    ``pg_advisory_xact_lock``; 5 seconds is long enough for normal acquisition
    (the lock is held only for a check-delete-insert sequence) and short enough
    to surface contention quickly. On timeout PostgreSQL raises
    ``statement_timeout`` (SQLSTATE 57014), surfacing as a 503.

    Generic over the wrapped callable's return type: callers wrap a
    load-and-mutate closure and expect its value back.
    """
    await session.execute(text(_GATE_LOCK_TIMEOUT_SQL))
    await session.execute(text(_GATE_ADVISORY_LOCK_SQL), {"key": policy_gate_lock_key(eval_id)})
    return await fn()
