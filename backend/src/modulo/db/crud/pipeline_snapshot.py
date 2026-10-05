"""Create immutable execution snapshots from the editable pipeline graph."""

import asyncio
import copy
import hashlib
import logging
import uuid
from collections.abc import Iterable
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from modulo.core.composite_engine.expander import expand_composites_in_graph
from modulo.core.exceptions import SnapshotLockNotAvailableError
from modulo.db.models.agent import Agent
from modulo.db.models.connector_instance import ConnectorInstance
from modulo.db.models.model_backend import ModelBackend
from modulo.db.models.parameter_schema import ParameterSchema
from modulo.db.models.parameter_set import ParameterSet
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.pipeline_edge import PipelineEdge
from modulo.db.models.pipeline_snapshot import PipelineSnapshot
from modulo.db.models.policy_gate import PolicyGate
from modulo.db.models.schema import Schema
from modulo.db.models.snapshot_schema_pin import SnapshotSchemaPin
from modulo.db.sqlstates import sqlstate_of
from modulo.db.url_utils import split_engine_sslmode
from modulo.settings import get_settings


def _ids(values: Iterable[Any]) -> set[uuid.UUID]:
    return {uuid.UUID(str(value)) for value in values if value is not None}


def _pipeline_lock_keys(pipeline_id: uuid.UUID) -> tuple[int, int]:
    """Derive two int4 advisory lock keys from a pipeline UUID using MD5."""
    digest = hashlib.md5(str(pipeline_id).encode("ascii"), usedforsecurity=False).digest()
    key1 = int.from_bytes(digest[:4], "big", signed=True)
    key2 = int.from_bytes(digest[4:8], "big", signed=True)
    return (key1, key2)


# FAR-527: bounded-wait acquisition of the snapshot advisory lock. Snapshot
# creation is a fast graph copy, so contention between two near-simultaneous
# run-starts resolves in milliseconds — a short retry loop nearly always
# succeeds where a single pg_try_advisory_lock attempt raised and the caller
# silently dropped the trigger. Module-level so tests can patch them.
SNAPSHOT_LOCK_ATTEMPTS = 5
SNAPSHOT_LOCK_RETRY_SLEEP_SECONDS = 0.25

# FAR-1287: bound on the WHOLE acquisition (connect + poll). The poll budget
# above is 5 x 0.25s of sleeps, so 5s is several times the normal case while
# staying far below the main pool's 30s ``pool_timeout`` — a saturated or dead
# lock source fails fast as SnapshotLockNotAvailableError instead of stalling a
# waiter (and a route) for half a minute. Module-level so tests can patch it.
_SNAPSHOT_LOCK_ACQUIRE_TIMEOUT_SECONDS = 5.0

# FAR-1287: one dedicated NullPool engine per source URL (see
# ``_dedicated_lock_engine``). Cached at module level like ``db.session``'s
# shared engine; a NullPool engine holds no pooled connections between calls,
# so the entry costs nothing but the engine object.
_SNAPSHOT_LOCK_ENGINES: dict[str, AsyncEngine] = {}

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# FAR-1287 Part 2: bounded OPTIMISTIC retry of the snapshot_version allocation
# ---------------------------------------------------------------------------
# The advisory lock is released in ``create_snapshot_from_live_graph``'s
# ``finally`` — BEFORE the caller's transaction commits — so a second creator can
# grab the lock inside that window, read the same ``max(snapshot_version)+1`` and
# collide on ``uq_pipeline_snapshot_version`` at insert time. The lock cannot be
# held across the caller's commit (it lives on its own connection and the caller
# owns the transaction), so the collision is resolved the optimistic way: a
# SAVEPOINT contains the read+insert, and an ``IntegrityError`` on that unique
# constraint is rolled back to the savepoint and retried with a freshly read
# version. Module-level so tests can patch the bound.
SNAPSHOT_VERSION_ATTEMPTS = 3

# The unique constraint whose violation identifies an allocation collision.
_SNAPSHOT_VERSION_CONSTRAINT = "uq_pipeline_snapshot_version"

# SQLSTATE ``insufficient_privilege`` — what Postgres raises when
# ``pg_terminate_backend`` is called by a role that is neither superuser nor a
# member of ``pg_signal_backend``.
_INSUFFICIENT_PRIVILEGE_SQLSTATE = "42501"


class SnapshotVersionAllocationError(IntegrityError):
    """Raised when the bounded snapshot_version allocation retry is exhausted.

    Subclasses ``IntegrityError`` on purpose: the collision it reports is
    exactly the unique-constraint failure callers already handle, so every
    existing ``except IntegrityError`` arm (route → 409 via ``handle_db_errors``,
    trigger engine, MCP) keeps its current mapping while this subclass carries a
    specific, actionable message. It is raised — never a silent ``None`` — after
    ``SNAPSHOT_VERSION_ATTEMPTS`` attempts, with the original driver error
    chained as ``__cause__``.
    """

    def __init__(self, pipeline_id: uuid.UUID, attempts: int) -> None:
        self.pipeline_id = pipeline_id
        self.attempts = attempts
        message = (
            f"could not allocate snapshot_version for pipeline {pipeline_id} after {attempts} attempts: "
            f"concurrent creators kept colliding on {_SNAPSHOT_VERSION_CONSTRAINT}"
        )
        super().__init__(message, {}, RuntimeError(message))


class SnapshotLockTerminateDeniedError(RuntimeError):
    """``pg_terminate_backend`` refused: the database role lacks the signal right.

    ``pg_terminate_backend`` requires superuser or membership in the
    ``pg_signal_backend`` predefined role. The endpoint that raises this stays
    live and answers with a typed 403 so an operator sees the exact grant to
    apply (``GRANT pg_signal_backend TO "<app role>";``) instead of a generic
    500. Raised by :func:`terminate_snapshot_lock_holders`.
    """

    def __init__(self, sqlstate: str | None) -> None:
        self.sqlstate = sqlstate
        super().__init__(
            "the database role is not permitted to terminate other backends: "
            "pg_terminate_backend requires superuser or "
            'membership in pg_signal_backend (GRANT pg_signal_backend TO "<app role>";)'
        )


def _snapshot_lock_engine(session: AsyncSession) -> AsyncEngine:
    """Resolve the engine that owns this caller's snapshot advisory locks.

    FAR-1287: the lock connection must NEVER be checked out of the caller's
    pool. Snapshot creation therefore draws it from a DEDICATED NullPool engine
    (:func:`_dedicated_lock_engine`) instead of ``session.bind``'s pool — so a
    burst of concurrent snapshot creations cannot consume a second main-pool
    slot per call and push every waiter into the 30s pool checkout timeout.

    The lock must also never live on the caller's *session*: the caller's
    transaction is frequently aborted by the time the ``finally`` runs (the
    ``ProgrammingError`` at snapshot-version read, an ``IntegrityError`` at
    ``flush``), and a ``pg_advisory_unlock`` issued from an aborted session is
    rejected (SQLSTATE 25P02 / SQLAlchemy ``PendingRollbackError``). Because the
    lock is SESSION-scoped, ROLLBACK does not release it either — the pooled
    connection goes back to the pool still holding it and snapshot creation
    fails permanently with ``snapshot_lock_busy`` (observed on app.modulo.run as
    HTTP 503 "Pipeline snapshot lock unavailable after 5 attempts").

    Engine derivation, verified against the pinned SQLAlchemy 2.1.1:
    ``AsyncSession.bind`` IS the ``AsyncEngine`` (or ``AsyncConnection``) the
    session was built from; ``AsyncSession.get_bind()`` is documented as
    "currently not used by AsyncSession" and returns the *sync*
    ``Engine``/``Connection``, which cannot drive an async connect. A session
    with no usable engine binding is a hard error, never a fallback to the
    caller's own connection.

    Module-level and side-effect-free: unit tests mock ``session.bind`` and
    monkeypatch :func:`_dedicated_lock_engine`, so they never reach a real engine.
    """
    bind = session.bind
    if isinstance(bind, AsyncEngine):
        return _dedicated_lock_engine(bind)
    raise RuntimeError(
        "create_snapshot_from_live_graph requires a session bound to an AsyncEngine so the "
        f"snapshot advisory lock can live on a dedicated connection; got {type(bind).__name__}"
    )


def _dedicated_lock_engine(bind: AsyncEngine) -> AsyncEngine:
    """Return the cached NullPool lock engine for *bind*'s database.

    Keyed by the caller engine's URL (including credentials), so the lock always
    lands in the SAME database the snapshot is written to — a lock keyed to some
    other URL would serialise nothing. One engine per distinct URL, exactly like
    ``db.session.get_shared_engine()``.

    ``poolclass=NullPool`` is what satisfies both FAR-1287 requirements at once:
    there are no pooled slots to contend (nothing to starve the main pool with,
    nothing to time out after 30s), and ``close()`` physically ends the PG
    session, so a released lock can never outlive its connection.

    This is the monkeypatch seam unit tests replace.
    """
    key = bind.url.render_as_string(hide_password=False)
    engine = _SNAPSHOT_LOCK_ENGINES.get(key)
    if engine is None:
        engine = _build_lock_engine(bind)
        _SNAPSHOT_LOCK_ENGINES[key] = engine
    return engine


def _build_lock_engine(bind: AsyncEngine) -> AsyncEngine:
    """Build the NullPool lock engine for *bind*'s database.

    Connect args mirror ``db.session._build_engine`` so a lock connection behaves
    like every other application connection and nothing can drift:

    * ``timeout=10`` — the app's connect timeout (the acquisition bound below
      is tighter anyway);
    * Postgres: ``statement_cache_size=0`` (the asyncpg prepared-statement cache
      is incompatible with HAProxy) and the deployment's TLS posture.

    The TLS value is read from ``settings.database_url``, NOT ``bind.url``:
    ``_build_engine`` has already split ``sslmode`` out of the URL it stores on
    the engine (FAR-1440), so reading it back from ``bind.url`` would always
    resolve to "absent" and silently downgrade a TLS deployment to plaintext.
    ``split_engine_sslmode`` is the single gate every asyncpg factory routes
    through, and it already resolved the deployment's posture once at boot.

    Deliberately NOT mirrored: pool sizing and ``pool_pre_ping`` — a NullPool
    engine has no pool to size and opens a fresh connection per checkout, so a
    ping would be a wasted round trip. No RLS reset hook either: this connection
    only ever runs advisory-lock SQL, never ORM queries.
    """
    connect_args: dict[str, Any] = {"timeout": 10}
    if str(bind.url.drivername).startswith("postgres"):
        # ssl is None exactly when settings is not a Postgres URL (SQLite/MySQL
        # deployments), whose drivers take none of these knobs.
        _, ssl = split_engine_sslmode(get_settings().database_url)
        if ssl is not None:
            connect_args["ssl"] = ssl
            connect_args["statement_cache_size"] = 0
    return create_async_engine(bind.url, poolclass=NullPool, connect_args=connect_args)


async def _dispose_snapshot_lock_connection(lock_conn: AsyncConnection, *, can_pool: bool) -> None:
    """Dispose the dedicated lock connection — ALWAYS, even under cancellation.

    ``can_pool=True`` (the lock is provably not held): a plain ``close()`` ends
    the physical session on the NullPool lock engine, which releases every
    advisory lock it holds.

    ``can_pool=False`` (the lock state is unknown — the unlock attempt was
    interrupted, or acquisition itself was interrupted): ``invalidate()`` is kept
    as belt-and-braces. On a POOLED engine this distinction is load-bearing
    (verified against real Postgres: pooled ``close()`` left the lock in
    ``pg_locks``, ``invalidate()`` dropped it to 0, only NullPool's ``close()``
    physically ends the session), and an unexpected non-NullPool engine must
    never be handed back to a pool still holding the lock.

    ``asyncio.shield`` is what keeps ``asyncio.CancelledError`` from stranding
    the connection: a cancellation delivered mid-dispose fails THIS ``await``
    but the dispose coroutine already wrapped by the shield keeps running to
    completion, so the lock cannot leak. The ``CancelledError`` still propagates
    afterwards (it is re-raised here), so the caller's own cancellation is
    never swallowed.
    """
    dispose = lock_conn.close() if can_pool else lock_conn.invalidate()
    try:
        await asyncio.shield(dispose)
    except asyncio.CancelledError:
        raise
    except Exception:
        # Best-effort teardown: report loudly (a failed teardown can strand a
        # lock) but never mask the caller's error.
        _log.warning("snapshot_lock_connection_dispose_failed", exc_info=True)


async def _open_and_poll_snapshot_lock(
    engine: AsyncEngine,
    *,
    pipeline_id: uuid.UUID,
    key1: int,
    key2: int,
) -> AsyncConnection:
    """Open a dedicated connection and poll for the per-pipeline advisory lock.

    Runs inside :func:`_acquire_snapshot_lock`'s ``asyncio.wait_for`` bound, so a
    lock source that hangs (dead host, saturated Postgres) is cancelled and
    disposed rather than left waiting. Keeps FAR-527's bounded-wait semantics:
    up to ``SNAPSHOT_LOCK_ATTEMPTS`` ``pg_try_advisory_lock`` attempts sleeping
    ``SNAPSHOT_LOCK_RETRY_SLEEP_SECONDS`` between them, raising
    ``SnapshotLockNotAvailableError`` once the budget is exhausted (mirrors
    ``core.runner_capacity._acquire_sweep_dedup_lock``).

    The returned connection HOLDS the lock and must be handed to
    :func:`_release_snapshot_lock`. Every path that leaves without it disposes
    the connection first, so an interrupted attempt can never strand the
    connection (and its lock).
    """
    lock_conn = await engine.connect()
    acquired = False
    exhausted = False
    try:
        for attempt in range(1, SNAPSHOT_LOCK_ATTEMPTS + 1):
            lock_result = await lock_conn.execute(
                text("SELECT pg_try_advisory_lock(:key1, :key2)"),
                {"key1": key1, "key2": key2},
            )
            if lock_result.scalar_one():
                acquired = True
                break
            if attempt < SNAPSHOT_LOCK_ATTEMPTS:
                await asyncio.sleep(SNAPSHOT_LOCK_RETRY_SLEEP_SECONDS)
        if not acquired:
            # Every attempt completed and reported "not held" and the loop ended
            # without a break, so the budget is exhausted and the connection is
            # provably lock-free when it is disposed below.
            exhausted = True
            raise SnapshotLockNotAvailableError(
                f"Cannot acquire snapshot lock for pipeline {pipeline_id} after {SNAPSHOT_LOCK_ATTEMPTS} attempts"
            )
    finally:
        if not acquired:
            # Budget exhausted cleanly -> the connection is provably lock-free;
            # a DB error or a cancellation mid-attempt -> the lock state is
            # unknown, so invalidate (physical close) rather than risk keeping a
            # holder alive.
            await _dispose_snapshot_lock_connection(lock_conn, can_pool=exhausted)
    return lock_conn


async def _acquire_snapshot_lock(
    session: AsyncSession,
    *,
    pipeline_id: uuid.UUID,
    key1: int,
    key2: int,
) -> AsyncConnection:
    """Acquire the per-pipeline snapshot advisory lock on a dedicated connection.

    Two bounds, both surfaced as ``SnapshotLockNotAvailableError`` (FAR-1287):

    * the POLITICAL bound — ``SNAPSHOT_LOCK_ATTEMPTS`` x ``SNAPSHOT_LOCK_RETRY_SLEEP_SECONDS``
      — covers ordinary contention between near-simultaneous run-starts;
    * the PHYSICAL bound — ``asyncio.wait_for`` around connect + poll — covers a
      lock source that never answers, so acquisition can never stall for a
      pool checkout timeout (30s) and then surface as a raw
      ``sqlalchemy.exc.TimeoutError``/503.

    Engine derivation happens OUTSIDE the bounded block so its loud
    ``RuntimeError`` (session has no usable engine binding) is never re-labelled
    as lock contention; genuine connection failures are logged with their
    traceback and then re-labelled, because an unavailable lock source is
    exactly what this contract reports.
    """
    engine = _snapshot_lock_engine(session)
    try:
        return await asyncio.wait_for(
            _open_and_poll_snapshot_lock(engine, pipeline_id=pipeline_id, key1=key1, key2=key2),
            timeout=_SNAPSHOT_LOCK_ACQUIRE_TIMEOUT_SECONDS,
        )
    except SnapshotLockNotAvailableError:
        raise
    except TimeoutError:
        # asyncio.wait_for's own bound, or a connect-level TimeoutError from the
        # driver — either way the lock source did not answer in time.
        _log.warning(
            "snapshot_lock_acquire_timeout pipeline_id=%s budget_s=%s",
            pipeline_id,
            _SNAPSHOT_LOCK_ACQUIRE_TIMEOUT_SECONDS,
        )
        raise SnapshotLockNotAvailableError(
            f"Cannot acquire snapshot lock for pipeline {pipeline_id}: lock source unavailable "
            f"within {_SNAPSHOT_LOCK_ACQUIRE_TIMEOUT_SECONDS}s"
        ) from None
    except Exception as exc:
        _log.warning("snapshot_lock_acquire_failed pipeline_id=%s", pipeline_id, exc_info=True)
        raise SnapshotLockNotAvailableError(
            f"Cannot acquire snapshot lock for pipeline {pipeline_id}: lock source unavailable ({type(exc).__name__})"
        ) from exc


async def _release_snapshot_lock(lock_conn: AsyncConnection, *, key1: int, key2: int) -> None:
    """Release the snapshot advisory lock on the SAME connection that holds it.

    The unlock is best-effort — it runs on a dedicated, healthy connection (the
    caller's aborted transaction can no longer poison it), but a cancellation or
    a transport error can still interrupt it — so the guaranteed release is the
    disposal in the inner ``finally``: ``close()`` once the unlock is confirmed,
    ``invalidate()`` (physical session teardown) when it is not.
    """
    unlocked = False
    try:
        await lock_conn.execute(
            text("SELECT pg_advisory_unlock(:key1, :key2)"),
            {"key1": key1, "key2": key2},
        )
        unlocked = True
    except Exception:
        # CancelledError is a BaseException and propagates untouched; any other
        # failure (transport, abort) leaves `unlocked=False`, which makes the
        # dispose below invalidate the physical session instead of pooling it.
        _log.warning("snapshot_lock_unlock_failed", exc_info=True)
    finally:
        await _dispose_snapshot_lock_connection(lock_conn, can_pool=unlocked)


# ---------------------------------------------------------------------------
# FAR-1287 Part 2 (Workstream A): operator inspection / clear path for a
# wedged snapshot advisory lock.
# ---------------------------------------------------------------------------
# ``pg_advisory_unlock`` only works on the connection that holds the lock, so a
# lock stranded by a dead or wedged backend CANNOT be unlocked from another
# session — the only primitive that clears it is terminating the holding
# backend. These two helpers derive the SAME keys :func:`_pipeline_lock_keys`
# derives for acquisition and read/act on ``pg_locks`` with those keys only.

# ``pg_locks.classid``/``objid`` are uint32: the signed int4 keys are recorded
# as a uint32 BIT-CAST (verified in Part 1 — ``pg_try_advisory_lock(-123456789)``
# lands at ``classid = 4171510507`` = ``-123456789 & 0xFFFFFFFF``, and binding
# the signed value fails with "value out of uint32 range"), so every predicate
# below binds the MASKED keys.
_SNAPSHOT_LOCK_HOLDER_SQL = text(
    "SELECT l.pid AS pid, a.application_name AS application_name, a.state AS state, "
    "a.backend_start AS backend_start, a.query_start AS query_start, l.granted AS granted "
    "FROM pg_locks AS l LEFT JOIN pg_stat_activity AS a ON a.pid = l.pid "
    "WHERE l.locktype = 'advisory' AND l.classid = :key1 AND l.objid = :key2 "
    "ORDER BY l.pid"
)

# Only GRANTED rows hold the lock — a waiter does not — and a backend is never
# asked to terminate itself (belt-and-braces: this session never acquires the
# snapshot lock, but a self-kill would take the request down mid-response).
_SNAPSHOT_LOCK_HOLDER_PIDS_SQL = text(
    "SELECT DISTINCT l.pid AS pid FROM pg_locks AS l "
    "WHERE l.locktype = 'advisory' AND l.classid = :key1 AND l.objid = :key2 "
    "AND l.granted AND l.pid <> pg_backend_pid() "
    "ORDER BY l.pid"
)

_TERMINATE_BACKEND_SQL = text("SELECT pg_terminate_backend(:pid) AS terminated")


def _masked_snapshot_lock_keys(pipeline_id: uuid.UUID) -> tuple[int, int]:
    """The pipeline's advisory keys, masked to the uint32 ``pg_locks`` domain."""
    key1, key2 = _pipeline_lock_keys(pipeline_id)
    return (key1 & 0xFFFFFFFF, key2 & 0xFFFFFFFF)


async def inspect_snapshot_lock(session: AsyncSession, pipeline_id: uuid.UUID) -> dict[str, Any]:
    """Report whether *pipeline_id*'s snapshot advisory lock is held, and by whom.

    Reads ``pg_locks`` (restricted to this pipeline's derived keys) joined to
    ``pg_stat_activity`` for the operator-facing detail. ``pg_locks`` is visible
    to every role; the ``pg_stat_activity`` detail columns may come back NULL for
    a viewer without ``pg_read_all_stats`` — every holder field is therefore
    optional and the diagnostic still answers the load-bearing question
    (``held``).

    Read-only: it neither acquires nor releases anything, so it is safe to call
    on a healthy pipeline.
    """
    key1, key2 = _masked_snapshot_lock_keys(pipeline_id)
    result = await session.execute(_SNAPSHOT_LOCK_HOLDER_SQL, {"key1": key1, "key2": key2})
    holders: list[dict[str, Any]] = [dict(row) for row in result.mappings().all()]
    return {"held": bool(holders), "holders": holders}


async def terminate_snapshot_lock_holders(session: AsyncSession, pipeline_id: uuid.UUID) -> dict[str, Any]:
    """Terminate ONLY the backend(s) currently holding this pipeline's lock.

    ``{"released": N, "pids": [...]}``; idempotent — no holder means
    ``released: 0`` and success. The pids come from a ``pg_locks`` read of the
    SAME derived keys (granted rows only, never this session), so a backend that
    does not hold exactly those keys can never be touched.

    A role without the signal right is reported as
    :class:`SnapshotLockTerminateDeniedError` (SQLSTATE 42501) rather than a raw
    driver error, so the route can answer with the grant it needs. Callers must
    hold a write transaction — an aborted transaction is not recoverable here.
    """
    key1, key2 = _masked_snapshot_lock_keys(pipeline_id)
    pid_result = await session.execute(_SNAPSHOT_LOCK_HOLDER_PIDS_SQL, {"key1": key1, "key2": key2})
    pids = [int(pid) for pid in pid_result.scalars().all()]

    terminated: list[int] = []
    for pid in pids:
        try:
            result = await session.execute(_TERMINATE_BACKEND_SQL, {"pid": pid})
        except SQLAlchemyError as exc:
            # Specific before base: only the privilege refusal becomes the typed
            # error; every other failure (transport, aborted txn) re-raises
            # untouched for the route's normal error mapping.
            if sqlstate_of(exc) == _INSUFFICIENT_PRIVILEGE_SQLSTATE:
                raise SnapshotLockTerminateDeniedError(_INSUFFICIENT_PRIVILEGE_SQLSTATE) from exc
            raise
        if bool(result.scalar_one()):
            terminated.append(pid)
        else:
            # Lost the race: the holder released/died between the read and the
            # terminate. Not a failure — the lock is gone either way.
            _log.info("snapshot_lock_terminate_race pid=%s pipeline_id=%s", pid, pipeline_id)
    return {"released": len(terminated), "pids": terminated}


async def _load_pipeline_and_edges(
    session: AsyncSession, pipeline_id: uuid.UUID
) -> tuple[Pipeline | None, list[dict[str, Any]], list[dict[str, Any]]]:
    pipeline_result = await session.execute(select(Pipeline).where(Pipeline.id == pipeline_id))
    pipeline = pipeline_result.scalar_one_or_none()
    if pipeline is None:
        return (None, [], [])
    edge_result = await session.execute(
        select(PipelineEdge)
        .where(PipelineEdge.pipeline_id == pipeline_id)
        .order_by(PipelineEdge.created_at, PipelineEdge.id)
    )
    edges = list(edge_result.scalars())
    nodes = copy.deepcopy(list(pipeline.graph_nodes_json or []))
    edge_dicts = [
        {
            "id": str(edge.id),
            "source": str(edge.source_node_id),
            "target": str(edge.target_node_id),
            "type": edge.edge_type,
            "hitl_review_config": copy.deepcopy(edge.hitl_review_config),
            "condition_expression": edge.condition_expression,
        }
        for edge in edges
    ]
    return (pipeline, nodes, edge_dicts)


def _apply_agent_fields(node: dict[str, Any], agent: Agent) -> uuid.UUID | None:
    if agent.token_budget is not None:
        node["token_budget"] = agent.token_budget
    if agent.prompt_template is not None:
        node["prompt_template"] = agent.prompt_template
    if agent.model_backend_id is not None:
        node["model_backend_id"] = str(agent.model_backend_id)
    if agent.agent_commands is not None:
        node["agent_commands"] = agent.agent_commands
    if agent.parameter_schema_id is not None:
        node["parameter_schema_id"] = str(agent.parameter_schema_id)
    # FAR-900: embed the agent-level schema_profile default so
    # _resolve_schema_profile in node_runner can read it without a DB query.
    if getattr(agent, "schema_profile", None) is not None:
        node.setdefault("schema_profile", agent.schema_profile)
    return agent.parameter_schema_id if agent.parameter_schema_id is not None else None


async def _materialize_agent_fields(
    session: AsyncSession, nodes: list[dict[str, Any]]
) -> tuple[list[Agent], dict[uuid.UUID, Agent], set[uuid.UUID]]:
    agent_ids = _ids(node.get("agent_id") for node in nodes)
    agents: list[Agent] = []
    agents_by_id: dict[uuid.UUID, Agent] = {}
    if agent_ids:
        agents = list((await session.execute(select(Agent).where(Agent.id.in_(agent_ids)))).scalars())
        agents_by_id = {a.id: a for a in agents}

    parameter_schema_ids: set[uuid.UUID] = set()
    for node in nodes:
        agent_id = node.get("agent_id")
        if agent_id is None:
            continue
        agent = agents_by_id.get(uuid.UUID(str(agent_id)))
        if agent is None:
            continue
        schema_id = _apply_agent_fields(node, agent)
        if schema_id is not None:
            parameter_schema_ids.add(schema_id)
    return (agents, agents_by_id, parameter_schema_ids)


async def _load_parameter_schemas(
    session: AsyncSession, parameter_schema_ids: set[uuid.UUID]
) -> dict[uuid.UUID, ParameterSchema]:
    schema_rows = (
        (await session.execute(select(ParameterSchema).where(ParameterSchema.id.in_(parameter_schema_ids))))
        .scalars()
        .all()
    )
    return {s.id: s for s in schema_rows}


def _collect_parameter_set_ids(nodes: list[dict[str, Any]]) -> set[uuid.UUID]:
    set_ids: set[uuid.UUID] = set()
    for node in nodes:
        raw_set_id = node.get("parameter_set_id")
        if raw_set_id is not None:
            set_ids.add(uuid.UUID(str(raw_set_id)))
    return set_ids


async def _load_parameter_sets(session: AsyncSession, set_ids: set[uuid.UUID]) -> dict[uuid.UUID, ParameterSet]:
    sets_by_id: dict[uuid.UUID, ParameterSet] = {}
    if set_ids:
        set_rows = (await session.execute(select(ParameterSet).where(ParameterSet.id.in_(set_ids)))).scalars().all()
        sets_by_id = {s.id: s for s in set_rows}
    return sets_by_id


def _resolve_node_parameters(
    schema: ParameterSchema, sets_by_id: dict[uuid.UUID, ParameterSet], node: dict[str, Any]
) -> dict[str, Any]:
    resolved: dict[str, Any] = {}
    for param in schema.parameters or []:
        if isinstance(param, dict) and "name" in param:
            resolved[param["name"]] = param.get("default")

    raw_set_id = node.get("parameter_set_id")
    if raw_set_id is not None:
        ps = sets_by_id.get(uuid.UUID(str(raw_set_id)))
        if ps is not None and isinstance(ps.values, dict):
            resolved.update(ps.values)

    overrides = node.get("parameter_overrides")
    if isinstance(overrides, dict):
        resolved.update(overrides)
    return resolved


def _build_parameter_binding(
    node: dict[str, Any], schema_id: uuid.UUID, resolved: dict[str, Any], raw_set_id: Any
) -> dict[str, Any]:
    return {
        "agent_id": str(node.get("agent_id")) if node.get("agent_id") else None,
        "parameter_schema_id": str(schema_id),
        "parameter_set_id": str(raw_set_id) if raw_set_id is not None else None,
        "resolved_values": resolved,
    }


async def _resolve_parameter_bindings(
    session: AsyncSession,
    nodes: list[dict[str, Any]],
    parameter_schema_ids: set[uuid.UUID],
) -> dict[str, Any]:
    parameter_bindings: dict[str, Any] = {}
    if parameter_schema_ids:
        schemas_by_id = await _load_parameter_schemas(session, parameter_schema_ids)
        set_ids = _collect_parameter_set_ids(nodes)
        sets_by_id = await _load_parameter_sets(session, set_ids)

        for node in nodes:
            raw_schema_id = node.get("parameter_schema_id")
            if raw_schema_id is None:
                continue
            schema_id = uuid.UUID(str(raw_schema_id))
            schema = schemas_by_id.get(schema_id)
            if schema is None:
                continue
            resolved = _resolve_node_parameters(schema, sets_by_id, node)
            node["_resolved_parameters"] = resolved
            raw_set_id = node.get("parameter_set_id")
            parameter_bindings[str(node["id"])] = _build_parameter_binding(node, schema_id, resolved, raw_set_id)
    return parameter_bindings


async def _load_reference_models(
    session: AsyncSession, nodes: list[dict[str, Any]], agents: list[Agent]
) -> tuple[dict[uuid.UUID, ConnectorInstance], dict[uuid.UUID, Schema], dict[uuid.UUID, ModelBackend]]:
    connector_ids = _ids(
        binding.get("instance_id") for node in nodes if (binding := node.get("connector_binding")) is not None
    )
    connectors: list[ConnectorInstance] = []
    if connector_ids:
        connectors = list(
            (await session.execute(select(ConnectorInstance).where(ConnectorInstance.id.in_(connector_ids)))).scalars()
        )
    connectors_by_id = {connector.id: connector for connector in connectors}

    schema_ids = {schema_id for agent in agents for schema_id in (agent.input_schema_id, agent.output_schema_id)}
    schemas: list[Schema] = []
    if schema_ids:
        schemas = list((await session.execute(select(Schema).where(Schema.id.in_(schema_ids)))).scalars())
    schema_models_by_id: dict[uuid.UUID, Schema] = {schema.id: schema for schema in schemas}

    backend_ids = {agent.model_backend_id for agent in agents if agent.model_backend_id is not None}
    backends: list[ModelBackend] = []
    if backend_ids:
        backends = list((await session.execute(select(ModelBackend).where(ModelBackend.id.in_(backend_ids)))).scalars())
    backends_by_id = {backend.id: backend for backend in backends}
    return (connectors_by_id, schema_models_by_id, backends_by_id)


def _build_connector_bindings(
    nodes: list[dict[str, Any]], connectors_by_id: dict[uuid.UUID, ConnectorInstance]
) -> list[dict[str, Any]]:
    connector_bindings: list[dict[str, Any]] = []
    for node in nodes:
        binding = node.get("connector_binding")
        if binding is None:
            continue
        connector_id = uuid.UUID(str(binding["instance_id"]))
        connector = connectors_by_id.get(connector_id)
        connector_bindings.append(
            {
                "node_id": str(node["id"]),
                "connector_instance_id": str(connector_id),
                "connector_type": (connector.connector_type_id if connector is not None else binding.get("type")),
                "instance_name": connector.name if connector is not None else None,
            }
        )
    return connector_bindings


def _build_schema_pins(agents: list[Agent], schema_models_by_id: dict[uuid.UUID, Schema]) -> list[dict[str, Any]]:
    schema_pins: list[dict[str, Any]] = []
    seen_schema_pins: set[tuple[uuid.UUID, str]] = set()
    for agent in agents:
        for schema_pin_id, schema_pin_version in (
            (agent.input_schema_id, agent.input_schema_version),
            (agent.output_schema_id, agent.output_schema_version),
        ):
            if schema_pin_id is None or schema_pin_version is None:
                continue
            key = (schema_pin_id, schema_pin_version)
            if key in seen_schema_pins:
                continue
            seen_schema_pins.add(key)
            schema_model = schema_models_by_id.get(schema_pin_id)
            schema_pins.append(
                {
                    "schema_id": str(schema_pin_id),
                    "version": schema_pin_version,
                    "abstract_name": schema_model.abstract_name if schema_model is not None else None,
                }
            )
    return schema_pins


def _build_prompt_and_backend_pins(
    agents: list[Agent], backends_by_id: dict[uuid.UUID, ModelBackend]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    prompt_pins = [
        {
            "agent_id": str(agent.id),
            "prompt_version_hash": hashlib.sha256(agent.prompt_template.encode()).hexdigest(),
            "prompt_version_at": agent.updated_at.isoformat(),
        }
        for agent in agents
    ]
    model_backend_pins = [
        {
            "agent_id": str(agent.id),
            "model_backend_id": str(agent.model_backend_id),
            "model_id": backend.model_id,
        }
        for agent in agents
        if agent.model_backend_id is not None and (backend := backends_by_id.get(agent.model_backend_id)) is not None
    ]
    return (prompt_pins, model_backend_pins)


async def _load_guardrail_pins(session: AsyncSession, pipeline: Pipeline) -> list[dict[str, Any]] | None:
    from modulo.core.guardrails import serialize_guardrail_pin
    from modulo.db.crud.guardrail_config import load_pipeline_guardrail_rows

    guardrail_rows = await load_pipeline_guardrail_rows(
        session,
        pipeline_id=pipeline.id,
        organisation_id=pipeline.organisation_id,
    )
    return [serialize_guardrail_pin(row) for row in guardrail_rows] or None


def _fingerprint_guardrail_pins(pins: list[dict[str, Any]] | None) -> str | None:
    """Canonical SHA-256 over the serialized guardrail pin set (FAR-309 PR B).

    Localized wrapper so the snapshot CRUD layer never reaches into the
    engine's internals — the fingerprint is computed by the shared guardrails
    module helper and only the digest is stored on the snapshot.
    """
    from modulo.core.guardrails import fingerprint_guardrail_pins

    return fingerprint_guardrail_pins(pins)


# ---------------------------------------------------------------------------
# Policy-gate snapshot pins (FAR-967 chunk 10, s3.1 - s3.3)
# ---------------------------------------------------------------------------


async def _load_policy_gate_rows_for_pipeline(
    session: AsyncSession,
    *,
    pipeline_id: uuid.UUID,
    organisation_id: uuid.UUID,
) -> list[PolicyGate]:
    """Load live, enabled PolicyGate rows bound to a pipeline's evals.

    Joins through ``evals`` (PolicyGate.eval_id → Eval.id) to scope to the
    pipeline.  Excludes soft-deleted gates AND disabled gates (§3.1 /
    criterion 14: disabled gates are NOT pinned at creation).
    """
    from modulo.db.models.eval import Eval

    stmt = (
        select(PolicyGate)
        .join(Eval, (PolicyGate.eval_id == Eval.id) & (PolicyGate.organisation_id == Eval.organisation_id))
        .where(
            Eval.pipeline_id == pipeline_id,
            PolicyGate.organisation_id == organisation_id,
            PolicyGate.deleted_at.is_(None),
            PolicyGate.enabled.is_(True),
        )
    )
    return list((await session.execute(stmt)).scalars().all())


def _build_policy_gate_pins(rows: list[PolicyGate]) -> list[dict[str, Any]]:
    """Serialize PolicyGate rows into snapshot pin entries (§3.2).

    Each entry carries ``policy_gate_id``, ``eval_id``, ``action``, and
    ``node_id`` — enough identity to reconstruct the gate's evaluation
    context and detect accidental corruption of the action field.

    A newly created snapshot ALWAYS stores a list: zero live/enabled gates
    produces ``[]`` (+ its digest), never ``NULL``.  ``NULL`` is reserved
    for genuinely pre-mechanism snapshots (rows that predate the pin
    columns) — an empty pin set is a deliberate "no gates pinned" state
    (§3.3 empty ≠ absent), so a gate disabled at snapshot time and
    re-enabled mid-run stays outside the evaluation universe (§5.4
    interleavings 1 and 3) instead of falling back to live gates.
    """
    return [
        {
            "policy_gate_id": str(row.id),
            "eval_id": str(row.eval_id),
            "action": row.action,
            "node_id": str(row.node_id),
        }
        for row in rows
    ]


def _fingerprint_policy_gate_pins(pins: list[dict[str, Any]] | None) -> str | None:
    """Canonical SHA-256 over the serialized policy-gate pin set (FAR-967 §3.3).

    Localized wrapper so the snapshot CRUD layer never reaches into the
    engine's internals — the fingerprint is computed by the shared
    ``fingerprint_policy_gate_pins`` helper and only the digest is stored
    on the snapshot.

    **Different empty-set semantics from the guardrail predecessor:**
    an empty list ``[]`` produces a deterministic digest (empty ≠ absent).
    """
    from modulo.core.eval_engine.policy_gate import fingerprint_policy_gate_pins

    return fingerprint_policy_gate_pins(pins)


def _add_snapshot_schema_pins(
    session: AsyncSession,
    organisation_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    nodes: list[dict[str, Any]],
) -> None:
    for node in nodes:
        raw_input_pin = node.get("input_schema_pin")
        raw_output_pin = node.get("output_schema_pin")
        if raw_input_pin is not None:
            session.add(
                SnapshotSchemaPin(
                    organisation_id=organisation_id,
                    snapshot_id=snapshot_id,
                    node_id=uuid.UUID(str(node["id"])),
                    direction="input",
                    schema_id=uuid.UUID(str(raw_input_pin["schema_id"])),
                    schema_version=str(raw_input_pin.get("schema_version", "")),
                )
            )
        if raw_output_pin is not None:
            session.add(
                SnapshotSchemaPin(
                    organisation_id=organisation_id,
                    snapshot_id=snapshot_id,
                    node_id=uuid.UUID(str(node["id"])),
                    direction="output",
                    schema_id=uuid.UUID(str(raw_output_pin["schema_id"])),
                    schema_version=str(raw_output_pin.get("schema_version", "")),
                )
            )


def _is_snapshot_version_conflict(exc: IntegrityError) -> bool:
    """True when *exc* is the ``uq_pipeline_snapshot_version`` unique violation.

    The retry must only ever retry the allocation collision it exists for: an
    unrelated integrity failure (FK, CHECK) would fail identically on every
    attempt and must propagate unchanged. Identified from the driver message
    (both asyncpg and psycopg name the violated constraint), with SQLSTATE
    ``23505`` + the column name as a dialect-tolerant fallback.
    """
    message = str(exc)
    if _SNAPSHOT_VERSION_CONSTRAINT in message:
        return True
    sqlstate = sqlstate_of(exc)
    return sqlstate == "23505" and "snapshot_version" in message


async def create_snapshot_from_live_graph(
    session: AsyncSession,
    *,
    pipeline_id: uuid.UUID,
    account_id: uuid.UUID | None = None,
    version_kind: str = "run",
    created_kind: str = "run",
    draft: bool = False,
    channel: str = "none",
) -> PipelineSnapshot | None:
    """Lock and copy the authoritative live graph into an immutable snapshot.

    The caller must already be inside a transaction with the organisation RLS
    context set. Uses a Postgres advisory lock (session-scoped) to serialise
    snapshot creation for a given pipeline, avoiding transaction-scoped FOR
    UPDATE so the caller's transaction is not blocked during graph loading.

    FAR-1287: the advisory lock lives on a DEDICATED connection opened from a
    dedicated NullPool lock engine (see :func:`_snapshot_lock_engine` and
    :func:`_dedicated_lock_engine`) and is released — unlock plus guaranteed
    disposal — on that same connection in the ``finally``. The caller's
    session/connection is never asked to acquire or release it, so a failure
    inside the copy (aborted caller transaction) can no longer leave the lock
    held by a pooled connection and wedge every later snapshot for that
    pipeline; and because the lock engine is not the caller's pool, concurrent
    snapshot creations never consume a second main-pool slot each.

    FAR-402 P6: the run-start callers (webhook/replay/trigger/manual/slack)
    keep the defaults and produce a ``version_kind='run'`` snapshot; live-edit
    saves go through ``create_snapshot_edit`` which passes ``version_kind='edit'``
    so the live-edit chain stays distinguishable from run-frozen snapshots.

    FAR-527: lock acquisition retries up to ``SNAPSHOT_LOCK_ATTEMPTS`` times,
    sleeping ``SNAPSHOT_LOCK_RETRY_SLEEP_SECONDS`` between attempts, so a
    near-simultaneous run-start (which holds the lock only for the fast graph
    copy) no longer fails the trigger outright. The whole acquisition is also
    bounded by ``_SNAPSHOT_LOCK_ACQUIRE_TIMEOUT_SECONDS``, so an unavailable
    lock source fails fast instead of stalling. Raises
    SnapshotLockNotAvailableError only after a bound is exhausted.

    FAR-1287 Part 2 (version-allocation race): the lock is still released in
    this function's ``finally``, i.e. BEFORE the caller's transaction commits —
    that ordering is unchanged and unavoidable (the lock lives on its own
    connection and the caller owns the commit). What Part 2 changed is the
    consequence: ``max(snapshot_version)+1`` and the insert now run inside a
    ``session.begin_nested()`` SAVEPOINT, and a collision on
    ``uq_pipeline_snapshot_version`` is rolled back to that savepoint and
    retried up to ``SNAPSHOT_VERSION_ATTEMPTS`` times with a freshly read max,
    so a concurrent same-pipeline creator can no longer surface
    ``IntegrityError`` at a caller. The window is still there; it is now
    benign. Exhausting the bound raises
    :class:`SnapshotVersionAllocationError` (an ``IntegrityError`` subclass, so
    every existing route/trigger handler keeps its 409 mapping) — never a
    silent ``None``.

    Retry scope (the documented choice): the GRAPH COPY — pipeline + edges,
    composite expansion, agent materialisation, parameter bindings, reference
    models — runs exactly once, outside the savepoint. Inside it, the version
    read, the guardrail/policy-gate pin loads and the insert are re-run on a
    retry. Those pin loads are two cheap SELECTs that already sit between the
    version read and the insert, so keeping them inside the savepoint means a
    retry can never assemble a row from a read set that straddles a rollback —
    and the first attempt issues statements in exactly the order it did before
    the retry existed. ``_add_snapshot_schema_pins`` runs inside the same
    savepoint, so a rolled-back attempt expunges its pin rows with the snapshot
    row: a retry can never duplicate them.
    """
    key1, key2 = _pipeline_lock_keys(pipeline_id)
    lock_conn = await _acquire_snapshot_lock(session, pipeline_id=pipeline_id, key1=key1, key2=key2)

    try:
        pipeline, nodes, edge_dicts = await _load_pipeline_and_edges(session, pipeline_id)
        if pipeline is None:
            return None

        # Expand composite nodes into flat sub-pipeline nodes BEFORE agent
        # materialization so sub-node agents get their prompt/model_backend
        # embedded like top-level nodes. After this the snapshot graph contains
        # only flat node types and the compiled runtime needs no changes.
        nodes, edge_dicts, composite_bindings = await expand_composites_in_graph(
            session,
            org_id=pipeline.organisation_id,
            nodes=nodes,
            edges=edge_dicts,
        )

        agents, _, parameter_schema_ids = await _materialize_agent_fields(session, nodes)
        parameter_bindings = await _resolve_parameter_bindings(session, nodes, parameter_schema_ids)
        connectors_by_id, schema_models_by_id, backends_by_id = await _load_reference_models(session, nodes, agents)

        # FAR-1287 Part 2: bounded OPTIMISTIC retry of the version allocation.
        # The graph copy above ran once; each attempt below re-reads
        # ``max(snapshot_version)``, re-loads the two pin sets and inserts,
        # ALL inside a SAVEPOINT so a collision on
        # ``uq_pipeline_snapshot_version`` rolls back to the savepoint — the
        # caller's outer transaction stays usable and the attempt re-runs with a
        # freshly read version instead of surfacing IntegrityError at a caller.
        for attempt in range(1, SNAPSHOT_VERSION_ATTEMPTS + 1):
            version_read_completed = False
            try:
                async with session.begin_nested():
                    try:
                        version_result = await session.execute(
                            select(func.coalesce(func.max(PipelineSnapshot.snapshot_version), 0)).where(
                                PipelineSnapshot.pipeline_id == pipeline_id
                            )
                        )
                        snapshot_version = int(version_result.scalar_one()) + 1
                    except ProgrammingError:
                        # Re-raised so the SAVEPOINT ROLLS BACK — catching it
                        # here would make the block exit cleanly and RELEASE the
                        # savepoint on an aborted transaction (25P02), masking
                        # the original error. Mapped to the historical ``None``
                        # outside the block.
                        raise
                    version_read_completed = True

                    connector_bindings = _build_connector_bindings(nodes, connectors_by_id)
                    schema_pins = _build_schema_pins(agents, schema_models_by_id)
                    prompt_pins, model_backend_pins = _build_prompt_and_backend_pins(agents, backends_by_id)

                    graph_json = {
                        "nodes": nodes,
                        "edges": edge_dicts,
                    }

                    # Guardrail snapshot pin (FAR-223 item 10): serialize the
                    # pipeline's bound guardrail rows so a replay evaluates the
                    # ORIGINAL conditions (the pinned set), never the live rows.
                    # Loaded here — not inside create_run — so the pin is
                    # immutable like the graph itself.
                    guardrail_pins = await _load_guardrail_pins(session, pipeline)
                    # Run-start snapshot-integrity fingerprint (FAR-309 PR B):
                    # the digest of the serialized pin set is saved alongside it
                    # so the replay seam can detect a tampered/drifted pin set
                    # and fail closed.
                    guardrail_pins_fingerprint = _fingerprint_guardrail_pins(guardrail_pins)

                    # FAR-967 chunk 10 (s3.1 - s3.3): policy-gate snapshot pins.
                    # Only live, enabled gates are pinned (criterion 14);
                    # disabled gates are excluded from the pin set at creation.
                    policy_gate_rows = await _load_policy_gate_rows_for_pipeline(
                        session,
                        pipeline_id=pipeline.id,
                        organisation_id=pipeline.organisation_id,
                    )
                    policy_gate_pins = _build_policy_gate_pins(policy_gate_rows)
                    policy_gate_pins_fingerprint = _fingerprint_policy_gate_pins(policy_gate_pins)

                    snapshot = PipelineSnapshot(
                        organisation_id=pipeline.organisation_id,
                        pipeline_id=pipeline.id,
                        snapshot_version=snapshot_version,
                        account_id=account_id,
                        graph_json=graph_json,
                        connector_bindings_json=connector_bindings,
                        schema_pins_json=schema_pins,
                        prompt_pins_json=prompt_pins,
                        model_backend_pins_json=model_backend_pins,
                        composite_bindings_json=composite_bindings or None,
                        parameter_bindings_json=parameter_bindings or None,
                        guardrail_pins_json=guardrail_pins,
                        guardrail_pins_fingerprint=guardrail_pins_fingerprint,
                        policy_gate_pins_json=policy_gate_pins,
                        policy_gate_pins_fingerprint=policy_gate_pins_fingerprint,
                        run_context_defaults=copy.deepcopy(pipeline.run_context_defaults),
                        default_autonomy_level=pipeline.default_autonomy_level,
                        max_autonomy_level=pipeline.max_autonomy_level,
                        stdout_retention_config=copy.deepcopy(pipeline.stdout_retention_config),
                        version_kind=version_kind,
                        created_kind=created_kind,
                        draft=draft,
                        channel=channel,
                    )
                    session.add(snapshot)
                    await session.flush()

                    # Inside the savepoint: a rolled-back attempt expunges these
                    # rows together with the snapshot row, so a retry can never
                    # duplicate them.
                    _add_snapshot_schema_pins(session, pipeline.organisation_id, snapshot.id, nodes)
                return snapshot
            except ProgrammingError:
                if version_read_completed:
                    raise
                # Pre-existing contract: a missing/unreadable snapshot_version
                # column (migration not applied yet) reports "no snapshot".
                return None
            except IntegrityError as exc:
                if not _is_snapshot_version_conflict(exc):
                    # Any other integrity failure (FK, RLS, pin constraints) is
                    # not an allocation collision — never swallowed, never
                    # retried: it would fail identically on every attempt.
                    raise
                if attempt == SNAPSHOT_VERSION_ATTEMPTS:
                    raise SnapshotVersionAllocationError(pipeline_id, SNAPSHOT_VERSION_ATTEMPTS) from exc
                _log.warning(
                    "snapshot_version_conflict_retry pipeline_id=%s attempt=%s/%s",
                    pipeline_id,
                    attempt,
                    SNAPSHOT_VERSION_ATTEMPTS,
                )
        # Unreachable by construction: the final attempt always returns or raises.
        raise SnapshotVersionAllocationError(pipeline_id, SNAPSHOT_VERSION_ATTEMPTS)
    finally:
        # Runs on every exit — success, `return None`, a ProgrammingError at the
        # version read, an IntegrityError at flush, or a cancellation — and the
        # unlock + disposal happen on the dedicated connection, never the
        # caller's (possibly aborted) session.
        await _release_snapshot_lock(lock_conn, key1=key1, key2=key2)


async def create_snapshot_edit(
    session: AsyncSession,
    *,
    pipeline_id: uuid.UUID,
    account_id: uuid.UUID | None = None,
    draft: bool = False,
    channel: str = "none",
) -> PipelineSnapshot | None:
    """Snapshot the live graph as a LIVE-EDIT version (FAR-402 P6).

    A live-edit save reuses the snapshot machinery but tags the row as
    ``version_kind='edit'`` / ``created_kind='edit'``, so the editor's save
    history (the live-edit chain) is distinguishable from run-frozen snapshots.
    Each save leaves the prior snapshot row immutable, so rollback remains a
    pointer swap to a prior snapshot (``rollback_to_snapshot``).
    """
    return await create_snapshot_from_live_graph(
        session,
        pipeline_id=pipeline_id,
        account_id=account_id,
        version_kind="edit",
        created_kind="edit",
        draft=draft,
        channel=channel,
    )
