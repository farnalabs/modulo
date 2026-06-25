"""Row-level security helpers.

All tenant scoping uses set_config('app.organisation_id', value, is_local=true),
which is equivalent to SET LOCAL and supports bound parameters. The semgrep rule
rls_set_local enforces that bare SET (without is_local) is never used.
"""

import logging
import uuid

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

_log = logging.getLogger(__name__)


async def set_rls_org(session: AsyncSession, org_id: uuid.UUID) -> None:
    """Activate RLS for *org_id* within the current transaction.

    Requires an active transaction — raises RuntimeError otherwise so callers
    cannot accidentally call this outside a BEGIN block and get silent no-ops.
    """
    if not session.in_transaction():
        raise RuntimeError("set_rls_org requires an active transaction; wrap the call in `async with session.begin():`")
    # set_config(name, value, is_local=true) is equivalent to SET LOCAL and
    # supports parameterised queries; bare SET LOCAL does not accept $1 placeholders.
    await session.execute(
        text("SELECT set_config('app.organisation_id', :oid, true)"),
        {"oid": str(org_id)},
    )


def register_rls_reset_hook(engine: AsyncEngine) -> None:
    """Register a pool-checkout listener that clears stale org context.

    Sets ``app.organisation_id`` to the empty string at session level whenever
    a connection is checked out from the pool.  Combined with set_config(...,
    is_local=true) (which reverts to the session-level value on transaction
    end), this guarantees no org_id leaks across requests sharing a pooled
    connection.

    Must be called once after the engine is created, typically in session.py.
    """

    @event.listens_for(engine.sync_engine, "checkout")
    def _reset_org_on_checkout(
        dbapi_connection: object,
        connection_record: object,
        connection_proxy: object,
    ) -> None:
        # SELECT is used instead of bare SET because asyncpg's synchronous DBAPI
        # shim routes statements through its cursor execute path, which requires
        # a query-style statement.
        with dbapi_connection.cursor() as cursor:  # type: ignore[attr-defined]
            try:
                cursor.execute("SELECT set_config('app.organisation_id', '', false)")
            except Exception:
                # Hook failure must not break connection checkout. Log and continue;
                # set_config(is_local=true) in set_rls_org still provides correct
                # transaction-scoped isolation even if this defensive reset is skipped.
                _log.warning(
                    "rls_reset_hook: failed to clear app.organisation_id on checkout",
                    exc_info=True,
                )
