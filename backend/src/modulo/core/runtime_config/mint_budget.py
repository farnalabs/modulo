"""Atomic per-organisation agent-mint budget (FAR-795).

A rate-control primitive guarding agent-sourced work-item minting: each spend
(n minted work items) is checked-and-recorded against a per-org rolling
budget window in ONE atomic SQL statement. There is no read-modify-write in
Python, so two concurrent minters can never jointly oversubscribe the limit:
the upsert's ``DO UPDATE ... WHERE used + :n <= :limit`` arm takes the row
lock for ``(org, window)`` and re-evaluates the predicate against the
committed value inside that lock — a consumer whose spend would exceed the
limit gets no row back and records nothing.

Design contract:

- **Storage** — ``org_mint_budget_usage(organisation_id, window_start, used)``
   keyed by ``(organisation_id, window_start)`` (committed by migration
   ``0239_org_mint_budget_usage``). Window rollover is natural: a new ``window_start`` is a new row,
  so usage resets with zero code.
- **Config** — limit + window length are env-overridable at read time
  (``MODULO_AGENT_MINT_BUDGET_LIMIT``, default 500;
  ``MODULO_AGENT_MINT_BUDGET_WINDOW_MINUTES``, default 60) because the
  runtime-config store's key set is process-boot-frozen and the SAQ worker
  and API processes consume this guard. Malformed values are ignored (log +
  default) rather than fatal.
- **Fail-OPEN** — any error raised by the guard itself (DB unavailable,
  dialect mismatch, driver error) returns ``True`` (allow) and logs the
  exception. This is the policy counterpart of :mod:`modulo.core.runtime_config.org_flags`:
  org_flags is a SAFETY kill-switch and fail-closes to OFF; the budget is a
  cost cap and must never become a hard dependency of run
  finalisation/reconciliation — an outage degrades cost control, never the
  pipeline. (If the budget
  table does not exist yet — e.g. a pre-migration SQLite environment — the
  statement fails and this fail-open path is what runs.)
- **No dialect guard needed for the statement** — ``INSERT ... ON CONFLICT
  DO UPDATE ... WHERE ... RETURNING`` is supported by both PostgreSQL and
  SQLite (≥3.35), so the single race-free statement runs unchanged on both
  (verified by the unit suite on ``aiosqlite`` and by the kill-switch test
  lane's real-Postgres probe); the migration creates the
  table on both dialects and applies the RLS/role ceremony on PostgreSQL only.
  Parameters are bound as canonical ISO-8601 strings (``organisation_id``
  str, ``window_start`` datetime.isoformat) — despite the raw ``text()``
  bypassing SQLAlchemy's per-dialect type processing, Postgres coerces them
  into the uuid/timestamptz target columns inside ``INSERT ... SELECT``
  context, while SQLite stores the ISO strings verbatim (PK equality does
  not care about string-vs-datetime storage).
- **Caller owns the transaction** — the usage row commits (or rolls back)
  with the caller's enclosing transaction, exactly like the other
  ``db/crud`` writers. Callers must place the RLS org context on the session
  (``set_rls_org``) on PostgreSQL, same contract as ``org_flags``.

RLS/tenant context: on PostgreSQL the caller (API route or SAQ worker
session) must set the org RLS context before calling — same contract as
:meth:`modulo.core.runtime_config.org_flags.read_org_flag` and the other
org-scoped readers in ``db/crud/run.py``.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text

_log = logging.getLogger(__name__)

# ── Config (env-overridable; read per call so operators don't need a restart) ──

ENV_AGENT_MINT_BUDGET_LIMIT = "MODULO_AGENT_MINT_BUDGET_LIMIT"
ENV_AGENT_MINT_BUDGET_WINDOW_MINUTES = "MODULO_AGENT_MINT_BUDGET_WINDOW_MINUTES"

#: Default budget: 500 agent-minted work items per organisation per hour.
DEFAULT_BUDGET_LIMIT = 500
#: Default window length in minutes (one hour).
DEFAULT_WINDOW_MINUTES = 60

# ── The single atomic statement ───────────────────────────────────────────
#
# One statement = the whole point. The SELECT-with-WHERE insert arm handles
# the first spend of a window (and rejects an ``n`` that is over the limit
# outright); the ON CONFLICT arm handles every later spend. The ``WHERE`` on
# DO UPDATE is evaluated under the conflicting row's lock AFTER the earlier
# transaction's increment has committed, so a spend that would push ``used``
# past ``:limit`` mutates nothing and returns no row → denied. ``... <=
# :limit`` (not ``<``) means the spend that exactly reaches the limit is the
# last one allowed — the budget is "at most :limit per window".
#
# ``CAST(... AS integer)`` pins the parameter types so Postgres can infer
# them in the bare ``SELECT ... WHERE`` (which has no column context) — the
# identical cast syntax is valid on SQLite, so one statement serves both.

_MINT_BUDGET_UPSERT_SQL = text(
    """INSERT INTO org_mint_budget_usage
       (organisation_id, window_start, used)
       SELECT :organisation_id, :window_start, CAST(:n AS integer)
       WHERE CAST(:n AS integer) <= CAST(:limit AS integer)
       ON CONFLICT (organisation_id, window_start)
       DO UPDATE SET used = org_mint_budget_usage.used + CAST(:n AS integer)
       WHERE org_mint_budget_usage.used + CAST(:n AS integer) <= CAST(:limit AS integer)
       RETURNING used"""
)


def _env_int(name: str, default: int, minimum: int) -> int:
    """Read an int env override; malformed/below-minimum values fall back."""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        _log.warning("mint_budget.malformed_env_int_ignored", extra={"env": name, "value": raw})
        return default
    if value < minimum:
        _log.warning("mint_budget.out_of_range_env_int_ignored", extra={"env": name, "value": value})
        return default
    return value


def current_window_start(window_minutes: int, *, now: datetime | None = None) -> datetime:
    """Floor ``now`` (UTC) to the start of the current budget window.

    Purely local (time.time()/UTC) — consistent for every process without a
    DB round-trip, so all writers agree on the same window key.
    """
    window = timedelta(minutes=max(1, window_minutes))
    reference = now or datetime.now(tz=UTC)
    elapsed = reference - datetime(1970, 1, 1, tzinfo=UTC)
    return datetime(1970, 1, 1, tzinfo=UTC) + (elapsed // window) * window


def _resolve_budget_config() -> tuple[int, int]:
    """Resolve (limit, window_minutes) from env with sane defaults."""
    limit = _env_int(ENV_AGENT_MINT_BUDGET_LIMIT, DEFAULT_BUDGET_LIMIT, minimum=1)
    window_minutes = _env_int(ENV_AGENT_MINT_BUDGET_WINDOW_MINUTES, DEFAULT_WINDOW_MINUTES, minimum=1)
    return limit, window_minutes


async def consume_agent_mint_budget(
    session: Any,
    org_id: uuid.UUID,
    n: int = 1,
) -> bool:
    """Atomically reserve ``n`` agent-mint spends against the org's budget.

    Returns ``True`` when the spend is within budget (and the usage row now
    carries it) or when the guard itself fails (fail-open — see the module
    docstring for why a cost cap must not block run finalisation). Returns
    ``False`` only when the spend would exceed the org's limit for the
    current window.

    A ``False`` result also means NO row was touched — a denied spend leaves
    the window's usage exactly as it was (unlike a read-then-write guard,
    which can leave an inconsistent tally under concurrency).

    The caller owns the session/transaction; callers on PostgreSQL must have
    the org RLS context set (``set_rls_org``). A ``(organisation_id,
    window_start)`` row is upserted for spent windows; old windows are
    cleaned up by the periodic housekeeping sweep (index
    ``ix_org_mint_budget_usage_window_start``).
    """
    if n <= 0:
        # Nothing to spend: consuming zero/negative items is vacuously in
        # budget and must not decrement a recorded tally.
        return True

    limit, window_minutes = _resolve_budget_config()
    try:
        window_start = current_window_start(window_minutes)
        # The statement is dialect-agnostic; only the BIND VALUE encoding
        # differs. Postgres/asyncpg infers the SELECT-list param as
        # timestamptz and requires a datetime instance; SQLite (aiosqlite)
        # has deprecated its default datetime adapter (the repo's unit
        # suite escalates that DeprecationWarning to an error), so SQLite
        # binds the canonical ISO string — which SQLite's loose typing
        # stores verbatim and PK equality is unaffected.
        try:
            dialect_name = session.get_bind().dialect.name
        except Exception:  # pragma: no cover - session without a bind
            dialect_name = "postgresql"
        window_start_value: datetime | str = (
            window_start if dialect_name.startswith("postgres") else window_start.isoformat()
        )
        result = await session.execute(
            _MINT_BUDGET_UPSERT_SQL,
            {
                "organisation_id": str(org_id),
                "window_start": window_start_value,
                "n": n,
                "limit": limit,
            },
        )
        row = result.first()
        allowed = row is not None
        if not allowed:
            _log.info(
                "mint_budget.exceeded",
                extra={"org_id": str(org_id), "n": n, "limit": limit, "window_minutes": window_minutes},
            )
        return allowed
    except Exception:
        # Fail-OPEN: a budget guard must never become a hard dependency of
        # the mint path. Log loudly so the outage is visible in the error
        # dashboard, then allow the spend.
        _log.exception("mint_budget.guard_failed_fail_open", extra={"org_id": str(org_id), "n": n})
        return True
