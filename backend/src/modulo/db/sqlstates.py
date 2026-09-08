"""SQLSTATE extraction + FAR-583 dual-write vocabularies (qa iteration 2).

Shared LEAF module: ``db.crud.run`` (the dual-write chokepoint's retry
decision) and ``core.pipeline_engine.node_runner`` (the marker-savepoint
failure classification) both consume :func:`sqlstate_of` and the vocabularies
below — previously two forked copies existed (``crud.run._sqlstate_of`` and
``node_runner._MARKER_TXN_ABORTING_SQLSTATES``). Deliberately leaf (imports
nothing from modulo) so both consumers import it without a cycle; the model
module (:mod:`modulo.db.models.run_node_outputs`) hosts other shared
constants, but this is DB-layer, not schema — a sibling leaf keeps the
vocabulary next to the extraction logic.

PG-FIDELITY (qa iteration 2, Major 2): the extraction walks ``__context__``,
not just ``.orig``/``__cause__``. On Postgres, a transaction-aborting failure
INSIDE a SQLAlchemy savepoint makes the savepoint's ``__aexit__`` raise the
ROLLBACK-TO-SAVEPOINT failure (SQLSTATE 25P02 — "in_failed_sql_transaction")
chained to the ORIGINAL driver error via ``__context__``. An extraction that
only inspects ``.orig``/``__cause__`` sees 25P02 and never the original
state — which misattributed :class:`~modulo.db.crud.run_node_outputs.
DualWriteError.sqlstate` and made the node-runner's transaction-aborting
classification (deadlock 40P01, shutdown, connection loss) dead code on the
exact failure it exists for.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "DUAL_WRITE_RETRYABLE_SQLSTATES",
    "MARKER_TXN_ABORTING_SQLSTATES",
    "SAVEPOINT_ROLLBACK_FAILURE_SQLSTATES",
    "sqlstate_of",
]

# The ONE bounded in-session retry of the new-table leg: ONLY the statement
# timeout (57014) is genuinely savepoint-recoverable — the transaction-aborting
# states (40001 serialization failure, 40P01 deadlock, 53300 connection
# exhaustion) abort the WHOLE transaction on Postgres, so re-entering a
# savepoint after catching them fails with 25P02 (a doomed extra round-trip);
# they go STRAIGHT to DualWriteError. Hard errors (42501 RLS, 23503 FK, 23505
# unique) also fail immediately. Authoritative copy (the dual-write
# orchestration deliberately does not mirror it).
DUAL_WRITE_RETRYABLE_SQLSTATES = frozenset({"57014"})

# SQLSTATEs whose failure aborts the WHOLE Postgres transaction (deadlock
# 40P01, admin shutdown 57P01, crash shutdown 57P02, connection-loss 08xxx
# classes — 08000/08001/08003/08004/08006/08007). A marker-savepoint failure
# with one of these has ALSO lost the legacy marker write (the outer
# transaction rolls back), so the "legacy survives, sweep heals" claim is
# false and the failure must be claimed loudly.
MARKER_TXN_ABORTING_SQLSTATES = frozenset(
    {"40P01", "57P01", "57P02", "08000", "08001", "08003", "08004", "08006", "08007"}
)

# The savepoint-rollback wrapper states: 25P02 ("in_failed_sql_transaction")
# is raised by ROLLBACK-TO-SAVEPOINT on an already-aborted transaction — it is
# ALWAYS a CONSEQUENCE of an earlier failure, never the root cause. The walk
# skips past it to report the original state (with 25P02 kept only as a
# last-resort fallback when the whole chain carries nothing else).
SAVEPOINT_ROLLBACK_FAILURE_SQLSTATES = frozenset({"25P02"})

# Bounded walk: the exception chain is walked through ``__context__`` /
# ``__cause__`` across at most this many nodes (cycle-safe via identity
# tracking). Real PG chains are 1-2 nodes deep (savepoint wrapper -> driver
# error); 8 is ample headroom without letting a pathological chain (or a
# cycle) walk forever.
_MAX_CHAIN_NODES = 8


def _state_of(candidate: Any) -> str | None:
    """The SQLSTATE of one candidate exception (asyncpg ``sqlstate`` /
    psycopg ``pgcode`` — dialect-tolerant)."""
    if candidate is None:
        return None
    state = getattr(candidate, "sqlstate", None) or getattr(candidate, "pgcode", None)
    return str(state) if state else None


def sqlstate_of(exc: BaseException) -> str | None:
    """Extract the SQLSTATE from a SQLAlchemy DBAPI error (dialect-tolerant).

    Per chain node the candidates are: the node ITSELF (a raw driver error
    carries ``sqlstate``/``pgcode`` directly), the SQLAlchemy ``.orig`` driver
    error, its ``__cause__``, and the node's own ``__cause__`` — a superset of
    the pre-hoist ``crud.run._sqlstate_of`` walk (the ``analytics.service.
    _is_query_canceled`` precedent). The walk then CONTINUES through BOTH
    ``__context__`` and ``__cause__`` (qa Major 2): a savepoint failure
    surfaces as the 25P02 rollback error with the ORIGINAL driver error as
    ``__context__``, and a caller-wrapped DBAPI error carries it as
    ``__cause__`` — the original state is the one that must be reported.

    25P02 is skipped in favour of any other state found anywhere in the chain
    (it can only be the rollback wrapper — a consequence, never the root);
    it is returned as a fallback only when the entire bounded chain carries
    nothing else. Cycle-safe (identity tracking) and bounded at
    ``_MAX_CHAIN_NODES`` visited nodes.
    """
    seen: set[int] = set()
    fallback: str | None = None
    queue: list[BaseException | None] = [exc]
    visited = 0
    while queue and visited < _MAX_CHAIN_NODES:
        node = queue.pop(0)
        if node is None or id(node) in seen:
            continue
        seen.add(id(node))
        visited += 1
        orig = getattr(node, "orig", None)
        for candidate in (
            node,
            orig,
            getattr(orig, "__cause__", None),
            getattr(node, "__cause__", None),
        ):
            state = _state_of(candidate)
            if state is None:
                continue
            if state not in SAVEPOINT_ROLLBACK_FAILURE_SQLSTATES:
                return state
            if fallback is None:
                fallback = state
        context_or_cause = (getattr(node, "__context__", None), getattr(node, "__cause__", None))
        queue.extend(child for child in context_or_cause if child is not None)
    return fallback
