"""FAR-1833 P1 regression — the cost endpoint must take the advisory BEFORE any
``organisations``-row mutation (prove-the-fix, real Postgres).

The D9 deadlock class this migration exists to eliminate can be reintroduced by a
lock-order inversion on the ADMIN endpoint: ``update_cost_controls`` used to call
``_cost_controls_patch`` (which sets ``org.daily_spend_limit`` — dirtying the ORM
``organisations`` row) BEFORE ``lock_org_cost_state``. That helper's first
statement is an ``execute`` (``set_config('lock_timeout', …)``), and the API
session autoflushes — so the org row lock was taken FIRST and the advisory was
requested SECOND. Finalize takes the opposite order (advisory first, then the
auto-pause ``organisations`` write), so the two formed an ABBA cycle: Postgres'
deadlock detector fires (SQLSTATE 40P01) at ~1 s, before the 5 s ``lock_timeout``
— an ABORT, not a bounded wait.

This test drives the REAL endpoint route function (not a mock, not a bare
``UPDATE organisations``) concurrently with the REAL finalize auto-pause org-row
write under the REAL per-org cost advisory lock, and asserts:

1. the endpoint's org-row write happens AFTER the advisory is held — observed by
   probing the org row with ``FOR NO KEY UPDATE NOWAIT`` while the endpoint is
   blocked on the advisory (it must NOT be row-locked by the endpoint), and
2. no side aborts with SQLSTATE 40P01 and the endpoint returns normally.

Without the P1 fix, assertion (1) fails (the endpoint holds the org row) AND the
cycle deadlocks (assertion (2) fails). Requires a live Postgres: no mock produces
the lock interaction or the SQLSTATE.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from modulo.api.routes import costs as costs_routes
from modulo.api.routes.costs import UpdateCostControlsRequest
from modulo.auth.jwt import TenantPrincipal
from modulo.core.cost_controller.org_auto_pause import (
    AUTO_PAUSE_REASON_SPEND_CEILING,
    maybe_auto_pause_org_triggers,
)
from modulo.core.runner_capacity import lock_org_cost_state
from modulo.db.models.organisation import Organisation
from modulo.db.rls import set_rls_org
from modulo.db.sqlstates import sqlstate_of

pytestmark = pytest.mark.integration

# Generous, deterministic margins: the endpoint's pre-lock work is a handful of
# fast queries (settles well under a second), and the finalize half waits long
# enough that the endpoint is provably blocked on the advisory before the
# org-row write happens. The deadlock detector fires ~1 s after the cycle forms,
# so the overall bound is comfortably above that.
_ENDPOINT_SETTLE_SECONDS = 1.5
_FINALIZE_WRITE_DELAY_SECONDS = 3.0
_FINALIZE_HOLD_SECONDS = 0.5
_OVERALL_TIMEOUT_SECONDS = 30.0

_SPEND_CEILING_CENTS = 100
_CUMULATIVE_CENTS = 99


async def _seed(db_engine: AsyncEngine) -> dict[str, Any]:
    """One org (auto-stop ON) with a cost-state row + one account."""
    org_id = uuid.uuid4()
    account_id = uuid.uuid4()
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO organisations (id, name, slug, settings_json, otel_config_json, triggers_paused) "
                "VALUES (:id, :name, :slug, "
                '        \'{"cost_controls": {"circuit_breaker_enabled": true}}\'::json, '
                "        '{}'::json, false)"
            ),
            {"id": str(org_id), "name": "FAR-1833 P1 org", "slug": f"far1833-p1-{org_id.hex[:8]}"},
        )
        await conn.execute(
            text(
                "INSERT INTO organisation_cost_state (org_id, spend_ceiling_cents, org_cumulative_spend_cents) "
                "VALUES (:id, :ceiling, :cumulative)"
            ),
            {"id": str(org_id), "ceiling": _SPEND_CEILING_CENTS, "cumulative": _CUMULATIVE_CENTS},
        )
        await conn.execute(
            text(
                "INSERT INTO accounts (id, email, display_name, password_hash, auth_provider, active) "
                "VALUES (:id, :email, :name, 'hash', 'local', true)"
            ),
            {"id": str(account_id), "email": f"far1833-p1-{account_id.hex[:8]}@example.com", "name": "FAR-1833 P1"},
        )
    return {"org_id": org_id, "account_id": account_id}


async def _cleanup(engine: AsyncEngine, seed: dict[str, Any]) -> None:
    org_id = seed["org_id"]
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM organisations WHERE id = :oid"), {"oid": str(org_id)})
        await conn.execute(text("DELETE FROM accounts WHERE id = :aid"), {"aid": str(seed["account_id"])})


async def _org_row_locked_nowait(engine: AsyncEngine, org_id: uuid.UUID) -> bool:
    """True iff the org row currently holds a conflicting row lock.

    ``FOR NO KEY UPDATE NOWAIT`` conflicts with the ``FOR NO KEY UPDATE`` an
    autoflushed ``UPDATE organisations`` takes, so a 55P03 here proves some
    other transaction is holding the org row. The probe always rolls back, so it
    leaves no lock behind.
    """
    locked = False
    sqlstate: str | None = None
    async with engine.connect() as conn:
        try:
            await conn.execute(
                text("SELECT 1 FROM organisations WHERE id = :oid FOR NO KEY UPDATE NOWAIT"),
                {"oid": str(org_id)},
            )
        except DBAPIError as exc:
            sqlstate = sqlstate_of(exc)
            locked = True
        await conn.rollback()
    if locked:
        assert sqlstate == "55P03", f"unexpected probe error: {sqlstate!r}"
    return locked


async def test_update_cost_controls_takes_advisory_before_org_row_write(db_engine: AsyncEngine) -> None:
    seed = await _seed(db_engine)
    org_id: uuid.UUID = seed["org_id"]
    principal = TenantPrincipal(
        username="far1833-p1",
        organisation_id=org_id,
        account_id=seed["account_id"],
        org_role="admin",
    )
    factory = async_sessionmaker(db_engine, expire_on_commit=False, autobegin=False)

    advisory_held = asyncio.Event()
    results: dict[str, Any] = {}

    async def finalize_half() -> None:
        """The REAL finalize half: advisory lock, then the auto-pause org write."""
        async with factory() as s2, s2.begin():
            await set_rls_org(s2, org_id)
            # The finalize ceiling gate's FIRST act (finalize.py:_apply_spend_ceiling_gate).
            await lock_org_cost_state(s2, org_id)
            advisory_held.set()
            # Give the endpoint time to reach its own first lock, then perform
            # the real finalize auto-pause org-row write (the other half of the
            # cycle) while still holding the advisory.
            await asyncio.sleep(_FINALIZE_WRITE_DELAY_SECONDS)
            org_row = await s2.get(Organisation, org_id)
            assert org_row is not None
            await maybe_auto_pause_org_triggers(
                s2,
                org=org_row,
                reason=AUTO_PAUSE_REASON_SPEND_CEILING,
                spend_cents=_SPEND_CEILING_CENTS + 1,
                limit_cents=_SPEND_CEILING_CENTS,
                run_id=uuid.uuid4(),
            )
            await s2.flush()
            await asyncio.sleep(_FINALIZE_HOLD_SECONDS)

    async def endpoint_half() -> None:
        """Drive the REAL ``update_cost_controls`` route function."""
        async with factory() as s1:
            req = UpdateCostControlsRequest(budget=5.0, max_run_cost=1.0)
            results["endpoint"] = await costs_routes.update_cost_controls(req, None, principal, s1)

    finalize_task = asyncio.create_task(finalize_half())
    endpoint_task: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(advisory_held.wait(), _OVERALL_TIMEOUT_SECONDS)
        endpoint_task = asyncio.create_task(endpoint_half())
        # Let the endpoint reach its first lock (and, pre-fix, take the org row).
        await asyncio.sleep(_ENDPOINT_SETTLE_SECONDS)
        # ORDERING PROOF: while the endpoint is blocked on the advisory it must
        # NOT hold the organisations row — the advisory precedes the org write.
        org_row_locked_by_endpoint = await _org_row_locked_nowait(db_engine, org_id)

        completed = await asyncio.wait_for(
            asyncio.gather(finalize_task, endpoint_task, return_exceptions=True),
            _OVERALL_TIMEOUT_SECONDS,
        )
    finally:
        for task in (finalize_task, endpoint_task):
            if task is not None and not task.done():
                task.cancel()
        await _cleanup(db_engine, seed)

    finalize_result, endpoint_result = completed
    for label, result in (("finalize", finalize_result), ("endpoint", endpoint_result)):
        if isinstance(result, BaseException):
            assert sqlstate_of(result) != "40P01", f"the {label} half deadlocked (40P01): {result!r}"
            raise AssertionError(f"the {label} half failed: {result!r}")

    # No 40P01 and the endpoint returned normally.
    assert "endpoint" in results, "the endpoint never returned a response"
    assert isinstance(results["endpoint"], costs_routes.CostControlsResponse)

    # The ordering assertion: pre-fix this is True (the endpoint holds the org
    # row before taking the advisory) and the cycle deadlocks.
    assert org_row_locked_by_endpoint is False, (
        "the endpoint mutated the organisations row BEFORE taking the cost advisory "
        "— the lock order is inverted (advisory must precede any org-row mutation)"
    )
