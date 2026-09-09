"""Runner capacity gate — atomic dispatch reservation (FAR-594 D8).

The pre-D8 dispatch decision was a check-then-act race: a lock-free count
followed (much) later by the sandbox provision, so a burst of concurrent
dispatches could each see ``active < cap`` and all provision. This module is
the atomic replacement:

* :func:`acquire_runner_dispatch_slot` — ONE transaction: own-row fenced lock
  FIRST (the conditional, claim-token-fenced marker UPDATE doubles as the row
  lock) → per-org advisory lock → lock-free count → decide → commit (the
  marker reflects the decision) → the caller provisions OUTSIDE the
  transaction. Uniform row→advisory ordering with the resume path
  (``executor.resume`` writes the run row before its advisory lock) makes the
  scheme cycle-free by construction — including same-run dispatch+resume
  overlap. The lock is NEVER held across workspace-create I/O.
* :func:`resolve_runner_cap_and_filter` — the tier-scoped cap decision shared
  by ALL capacity paths (dispatch gate, resume gate, HITL pre-check,
  claim-time read). Uses the FAR-589 D3b reader contract
  (``SandboxConcurrencyLimit``): the flag-off window enforces ``enforced_cap``
  (absent key = NO gate); the D8 rollout flag activates the Docker-tier
  default (absent key → 4, counting Docker+Local providers only; legacy
  tier-less markers count as Docker — fail-safe).
* :func:`reconcile_runner_dispatch_markers` — the state-aware reconciliation
  sweep (D1 cadence, wired into ``dispatcher_reconcile``): clears non-fence
  markers on terminal runs and stale (>25h) markers, transitions a stale
  non-terminal ``running`` run terminal (no zombie
  running-without-marker-without-workspace), never touches
  ``script_executing`` fence components or runs ``dispatcher_reconcile``
  considers recoverable (checked with the reconciler's OWN SQL predicates —
  parity by construction), and asserts the live count ≤ cap (the D8 rollback
  signal). Every per-row clear/transition UPDATE is CAS-guarded on the
  classified marker text (a concurrent fresh-marker commit makes the UPDATE
  match 0 rows — the sweep never clobbers a marker it did not classify), the
  candidate scan is batched with a cursor, recoverability is evaluated lazily
  (never for terminal rows — the recovery predicates are status-scoped), and
  any org-index or org-pass failure raises :class:`RunnerMarkerSweepError`
  carrying the partial counts (the SAQ cron wrapper persists them and
  re-raises so SAQ retries engage).
* :func:`mark_runner_dispatch_cleared_at_hitl` — the HITL-boundary tombstone:
  at interrupt handling (before the run enters ``awaiting_human``) any
  remaining dispatch marker becomes ``{"state": "cleared_at_hitl"}`` —
  capacity-neutral (the count is running-only) while remaining visible to the
  rollback detector.

Lock namespace: a RESERVED constant prefix (``modulo:runner-capacity:org-v1``)
hashed with the org id — NEVER the shared ``_uuid_to_lock_keys`` keyspace
(the legacy derivation hashes the raw UUID, so its keys collide with every
connector/trigger lock for the same id) and never a single global key. The
sweep dedup lock uses a DISTINCT derivation suffix so it can never deadlock
against a per-org gate key; it is taken SESSION-scoped on a DEDICATED
``engine.connect()`` connection held for the sweep's lifetime, using
``pg_try_advisory_lock`` + bounded polling (the codebase convention shared by
``api.main._migration_advisory_lock`` and ``db.migrations.env._migration_advisory_lock``
— a bare blocking ``pg_advisory_lock`` races server-side acquisition against the
client timeout). The lock is released on THAT SAME connection in the sweep's
finally, so a leaked lock can never hang a later sweep indefinitely.

Failure policy (per class): at-capacity and lock-timeout (SQLSTATE 55P03)
denials raise :class:`RunnerCapacityDeniedError` (retryable — the caller maps
it to ``SandboxCapacityExceededError`` / ``capacity.org``); a genuine 40P01
deadlock is a separate ``runner.capacity.deadlock_degraded`` alarm (expected
impossible under the uniform ordering); every OTHER DB error fails OPEN with a
``runner.capacity.gate_error`` event (matching the pre-D8 behaviour — a DB
hiccup must never become a dispatch outage), and the caller still writes the
dispatch marker best-effort in its own transaction so a fail-open dispatch is
never markerless.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from modulo.db.models.run import TERMINAL_STATUSES
from modulo.db.sqlstates import sqlstate_of
from modulo.settings import get_settings

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lock namespace (reserved prefix — never the shared _uuid_to_lock_keys keyspace)
# ---------------------------------------------------------------------------

# Reserved namespace for the per-org runner-capacity keys. The legacy
# ``_uuid_to_lock_keys`` derivation hashes the raw UUID, so its keyspace is
# shared with connector/trigger/system-config locks — a capacity gate on it
# could block or be blocked by an unrelated connector write for the same org.
# FORBIDDEN for this gate (dist spec D8); the org id is hashed INSIDE this
# reserved prefix instead.
_RUNNER_CAPACITY_LOCK_NAMESPACE = "modulo:runner-capacity:org-v1"
# DISTINCT derivation suffix for the sweep's dedup lock: a different namespace
# string means a sweep tick can never contend with (or deadlock against) a
# per-org gate key.
_RUNNER_MARKER_SWEEP_LOCK_NAMESPACE = "modulo:runner-capacity:sweep-v1"

_INT4_BYTES = 4


def _namespace_lock_keys(namespace: str, subject: str) -> tuple[int, int]:
    """Derive two stable int4 advisory-lock keys from ``namespace ⊕ subject``.

    MD5 (non-security) of ``namespace + ":" + subject`` split into two signed
    32-bit ints — the same two-key shape as the shared lock service but
    namespaced, so a runner-capacity key can never collide with a key from a
    different subsystem or the legacy raw-UUID keyspace.
    """
    digest = hashlib.md5(f"{namespace}:{subject}".encode(), usedforsecurity=False).digest()
    key1 = int.from_bytes(digest[:_INT4_BYTES], "big", signed=True)
    key2 = int.from_bytes(digest[_INT4_BYTES : _INT4_BYTES * 2], "big", signed=True)
    return (key1, key2)


def runner_capacity_lock_keys(org_id: uuid.UUID) -> tuple[int, int]:
    """Per-org advisory-lock keys for the dispatch/resume capacity gates."""
    return _namespace_lock_keys(_RUNNER_CAPACITY_LOCK_NAMESPACE, str(org_id))


def runner_marker_sweep_lock_keys() -> tuple[int, int]:
    """The sweep dedup lock keys — a DISTINCT suffix from the per-org keys."""
    return _namespace_lock_keys(_RUNNER_MARKER_SWEEP_LOCK_NAMESPACE, "sweep-dedup")


# ---------------------------------------------------------------------------
# Marker vocabulary (runs.sandbox_dispatch_state JSON)
# ---------------------------------------------------------------------------

RUNNER_PROVIDER_DOCKER = "runner_docker"
RUNNER_PROVIDER_E2B = "e2b"
RUNNER_PROVIDER_LOCAL = "local"

MARKER_STATE_DISPATCHING = "dispatching"
MARKER_STATE_SCRIPT_EXECUTING = "script_executing"
MARKER_STATE_CLEARED_AT_HITL = "cleared_at_hitl"

# Providers billed against the HOST (Docker containers / local subprocesses).
# The absent-key Docker-tier default counts ONLY these (+ legacy tier-less
# markers, which are Docker-tier by fail-safe); e2b has its own platform-side
# concurrency quota and is excluded from that default bucket.
HOST_RESOURCE_PROVIDERS: frozenset[str] = frozenset({RUNNER_PROVIDER_DOCKER, RUNNER_PROVIDER_LOCAL})

# SQL provider attribution: markers carrying a JSON "provider" key are
# attributed from it; legacy tier-less markers (pre-D8 JSON without the key,
# and the bare "dispatching" literal) attribute to runner_docker — fail-safe
# (they can only be E2B-legacy or Docker, and Docker is the tier the
# default-cap bucket must not undercount). The single-sourced SQL fragments
# live in ``modulo.db.crud.run`` (the DB layer owns the count body; core
# imports db, never the reverse) — ``RUNNER_TOMBSTONE_EXCLUSION_SQL`` and
# ``RUNNER_HOST_RESOURCE_FILTER_SQL`` there. The tombstone exclusion matches
# the ``state`` field PRECISELY (``"state": "cleared_at_hitl"``) — a bare
# ``%cleared_at_hitl%`` substring would also exclude a hypothetical marker
# whose attempt key merely contains the literal.


def build_dispatch_marker(attempt_key: str, provider: str | None = None) -> str:
    """The structured ``runs.sandbox_dispatch_state`` dispatch marker (D8 shape).

    Base shape (unchanged, fence-compatible — ``_script_lease_probe_ok`` /
    ``rollback_thresholds`` / ``dispatcher_reconcile`` read only
    ``state``/``attempt_key``): ``{"state": "dispatching", "attempt_key": …}``.
    The D8 gate adds ``"provider"`` (``runner_docker`` | ``e2b`` | ``local``)
    and ``"written_at"`` for every gated tier. A tier-less marker (provider
    omitted — the Bundled Runner path's call shape) counts as Docker-tier.
    """
    marker: dict[str, Any] = {"state": MARKER_STATE_DISPATCHING, "attempt_key": attempt_key}
    if provider:
        marker["provider"] = provider
        marker["written_at"] = datetime.now(UTC).isoformat()
    return json.dumps(marker)


def build_hitl_tombstone() -> str:
    """The capacity-neutral HITL-boundary tombstone (``not NULL``).

    Replaces a live dispatch marker when the run parks at a gate so the parked
    run holds no slot (the count is ``running``-only AND tombstone-excluded)
    while the row still records that a sandbox dispatch happened.
    """
    return json.dumps({"state": MARKER_STATE_CLEARED_AT_HITL, "written_at": datetime.now(UTC).isoformat()})


def parse_marker_state(marker_json: str | None) -> str | None:
    """Best-effort ``state`` extraction from a marker (never raises)."""
    if not marker_json:
        return None
    try:
        parsed = json.loads(marker_json)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    state = parsed.get("state")
    return state if isinstance(state, str) else None


def parse_marker_provider(marker_json: str | None) -> str | None:
    """Best-effort ``provider`` extraction (``None`` = legacy tier-less)."""
    if not marker_json:
        return None
    try:
        parsed = json.loads(marker_json)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    provider = parsed.get("provider")
    return provider if isinstance(provider, str) else None


def parse_marker_written_at(marker_json: str | None) -> datetime | None:
    """Best-effort ``written_at`` extraction (``None`` = legacy tier-less)."""
    if not marker_json:
        return None
    try:
        parsed = json.loads(marker_json)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    raw = parsed.get("written_at")
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def marker_is_fence_component(marker_json: str | None) -> bool:
    """True when the marker carries the ``script_executing`` exactly-once fence.

    The sweep NEVER clears fence-carrying markers (rule (a) — precedence over
    the staleness rule): the fence proves a script PROCESS may have started.
    """
    return parse_marker_state(marker_json) == MARKER_STATE_SCRIPT_EXECUTING


# ---------------------------------------------------------------------------
# Capacity decision (one reader for every capacity path)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunnerCapacityDecision:
    """The resolved capacity decision shared by all four capacity paths.

    ``cap is None`` = no gate (``active`` short-circuits to 0 — the count is
    only consumed for the breach verdict). ``host_resource_only`` marks the
    Docker-tier default scope (the count must filter to
    ``HOST_RESOURCE_PROVIDERS``); legacy tier-less markers count into that
    bucket via the SQL attribution. The count POPULATION is flag-dependent:
    flag-on narrows to ``running``-only with the tombstone exclusion (D8);
    flag-off keeps the pre-D8 ``ACTIVE_RUN_STATUSES`` population exactly.
    """

    cap: int | None
    active: int
    host_resource_only: bool


async def read_runner_cap_contract(session: AsyncSession, org_id: uuid.UUID) -> tuple[int | None, bool]:
    """(cap, host_resource_only) from the D3b reader + the D8 flag.

    Flag OFF: the merged flag-window contract — ``enforced_cap`` only (the
    absent-key Docker-tier default does NOT gate until the rollout flag
    activates it), unfiltered count. Flag ON: absent key → the Docker-tier
    default counting host-resource providers only; explicit int → all tiers;
    explicit ``null`` (and fail-open) → no gate.
    """
    from modulo.db.crud.run import get_sandbox_concurrency_limit

    limit = await get_sandbox_concurrency_limit(session, org_id)
    if not get_settings().runner_capacity_gate_enabled:
        return limit.enforced_cap, False
    if limit.cap is None:
        return None, False
    if limit.is_default:
        return limit.cap, True
    return limit.cap, False


async def count_active_runner_dispatches_for_decision(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    exclude_run_id: uuid.UUID | None = None,
    host_resource_only: bool = False,
) -> int:
    """Thin wrapper over :func:`modulo.db.crud.run.count_active_runner_dispatches_for_org`
    with the tier filter applied. Kept here so every capacity path resolves
    cap + count through ONE module."""
    from modulo.db.crud.run import count_active_runner_dispatches_for_org

    return await count_active_runner_dispatches_for_org(
        session,
        org_id,
        exclude_run_id=exclude_run_id,
        host_resource_only=host_resource_only,
    )


async def resolve_runner_capacity_decision(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    exclude_run_id: uuid.UUID | None = None,
) -> RunnerCapacityDecision:
    """Read the cap AND the matching count in one call (lock-free reads).

    The SINGLE decision path for every capacity consumer (the gate after its
    advisory lock, the lock-free HITL pre-check, the claim-time read, the
    sweep's violation assert). The count POPULATION is flag-dependent inside
    the count body: flag-on = the narrowed D8 population (``running`` runs
    with a live marker, tombstone-excluded, own marker excluded); flag-off =
    the pre-D8 ``ACTIVE_RUN_STATUSES`` population exactly. ``cap is None``
    short-circuits to ``active=0`` — no gate means no count is needed (the
    only consumer of ``active`` is the breach verdict).
    """
    cap, host_resource_only = await read_runner_cap_contract(session, org_id)
    if cap is None:
        return RunnerCapacityDecision(cap=None, active=0, host_resource_only=host_resource_only)
    active = await count_active_runner_dispatches_for_decision(
        session,
        org_id,
        exclude_run_id=exclude_run_id,
        host_resource_only=host_resource_only,
    )
    return RunnerCapacityDecision(
        cap=cap,
        active=active,
        host_resource_only=host_resource_only,
    )


# ---------------------------------------------------------------------------
# Exceptions + gate result
# ---------------------------------------------------------------------------


class RunnerCapacityDeniedError(RuntimeError):
    """Retryable capacity denial: at cap, or the advisory lock timed out (55P03).

    The caller translates this to the house retryable capacity failure
    (``SandboxCapacityExceededError`` → ``capacity.org``) so the node is
    re-dispatched when capacity frees — never a terminal failure.
    """


@dataclass(frozen=True)
class RunnerDispatchSlot:
    """The gate's outcome.

    ``acquired`` — slot reserved AND the dispatch marker committed in the gate
    transaction; the caller provisions outside it. ``fenced`` — the claim is
    superseded or the run is not running; no sandbox may be created.
    ``fail_open`` — a DB error (or missing claim context) degraded the gate;
    the caller falls back to the legacy best-effort marker write and
    dispatches (fail-open, matching the pre-D8 behaviour).
    """

    status: Literal["acquired", "fenced", "fail_open"]
    attempt_key: str | None
    marker_set: bool


async def acquire_runner_dispatch_slot(
    session_factory: Any,
    *,
    org_id: uuid.UUID,
    run_id: str,
    claim_token: str | None,
    node_id: str,
    provider: str | None = None,
) -> RunnerDispatchSlot:
    """The atomic runner dispatch gate (D8) — check and reserve in ONE transaction.

    Transaction shape (uniform row→advisory ordering — cycle-free with the
    resume path, which writes the run row before its advisory lock):

    1. ``SET LOCAL lock_timeout`` (env-tunable, default 2s) — a crowded
       advisory lock degrades to a RETRYABLE denial instead of hanging on
       ``deadlock_timeout``.
    2. Lock THIS run's own row FIRST — a claim-token-fenced
       ``SELECT ... FOR UPDATE``; the fenced marker UPDATE doubles as the row
       lock. Rowcount 0 = superseded / not running → fenced.
    3. Per-org advisory lock (reserved-prefix derivation).
    4. Lock-free count (running runs with a live marker, own marker excluded,
       tier filter for the Docker-tier default) + cap read.
    5. Decide: deny → :class:`RunnerCapacityDeniedError` (the transaction
       rolls back — no marker, no slot); admit → the fenced marker UPDATE
       commits the reservation (provider + written_at ride the marker).

    The lock is NEVER held across workspace-create I/O — the caller provisions
    after this transaction commits.

    Never raises for DB failures: they fail OPEN (``fail_open``) with a
    ``runner.capacity.gate_error`` event; the caller's best-effort marker
    write still runs so a fail-open dispatch stays counted wherever the
    marker write can commit (and markerless fail-open when even that write
    fails — qa F11; a DB hiccup must never become a dispatch outage).
    """
    if session_factory is None or not claim_token:
        # Fail-open: no claim context — the legacy fail-open marker path in
        # the caller handles both the attempt key and the best-effort write.
        return RunnerDispatchSlot("fail_open", None, False)
    settings = get_settings()
    lock_timeout_ms = settings.runner_capacity_lock_timeout_ms
    flag_on = settings.runner_capacity_gate_enabled
    # Tier attribution normalisation: a tier-less caller (provider omitted)
    # attributes to the Docker tier — EXACTLY matching the count body's SQL
    # attribution (``COALESCE(provider_key, 'runner_docker')``), so the skip
    # condition below can never admit a dispatch the count will count. An
    # UNKNOWN provider value (e.g. a future tier) is likewise skipped by the
    # Docker-tier default gate — the host-resource count does not count it.
    provider = provider or RUNNER_PROVIDER_DOCKER
    from modulo.db.rls import set_rls_execution_context, set_rls_org

    try:
        async with session_factory() as session, session.begin():
            await set_rls_org(session, org_id)
            await set_rls_execution_context(session)
            if flag_on:
                await session.execute(
                    text("SELECT set_config('lock_timeout', :val, true)"),
                    {"val": f"{lock_timeout_ms}ms"},
                )
            # (2) own-row lock FIRST — claim-token-fenced, status-guarded.
            row = (
                await session.execute(
                    text(
                        "SELECT claim_count FROM runs "
                        "WHERE id = :rid AND organisation_id = :oid "
                        "AND claim_token = :tok AND status = 'running' "
                        "FOR UPDATE"
                    ),
                    {"rid": run_id, "oid": str(org_id), "tok": claim_token},
                )
            ).fetchone()
            if row is None:
                return RunnerDispatchSlot("fenced", None, False)
            attempt_key = f"run:{run_id}:node:{node_id}:{int(row[0])}"
            # The count excludes the run's OWN marker. A non-UUID run_id can
            # never match a runs.id (the own-row SELECT above would have
            # fenced first), so a parse failure degrades to no-exclusion
            # rather than failing the gate.
            try:
                run_uuid: uuid.UUID | None = uuid.UUID(str(run_id))
            except (TypeError, ValueError):
                run_uuid = None
            if flag_on:
                # (3) per-org advisory lock — reserved-prefix derivation.
                k1, k2 = runner_capacity_lock_keys(org_id)
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(:k1, :k2)"),
                    {"k1": k1, "k2": k2},
                )
            # (4) lock-free count + tier-scoped cap (own marker excluded —
            # a re-dispatch of a run still carrying a fence-carrying stale
            # marker cannot self-block). The SINGLE decision path for both
            # flag states: the count body owns the flag-dependent population
            # (flag-off = the pre-D8 ACTIVE_RUN_STATUSES population, no
            # advisory lock, no lock_timeout — the pre-D8 racy check-then-act
            # semantics exactly).
            decision = await resolve_runner_capacity_decision(session, org_id, exclude_run_id=run_uuid)
            if decision.cap is not None and decision.active >= decision.cap:
                # The Docker-tier default (absent key) gates ONLY host-resource
                # dispatches: the skip condition mirrors the count scope — any
                # provider NOT counted into the host-resource bucket (e2b, and
                # unknown future tiers) is neither counted into that bucket nor
                # denied by it. Legacy tier-less dispatchers normalise to
                # Docker above and stay gated.
                docker_default_skips_gate = decision.host_resource_only and provider not in HOST_RESOURCE_PROVIDERS
                if not docker_default_skips_gate:
                    _log.warning(
                        "runner.capacity.denied",
                        extra={
                            "run_id": run_id,
                            "org_id": str(org_id),
                            "node_id": node_id,
                            "active": decision.active,
                            "cap": decision.cap,
                            "host_resource_only": decision.host_resource_only,
                        },
                    )
                    raise RunnerCapacityDeniedError(
                        f"Runner dispatch denied: org {org_id} at capacity "
                        f"({decision.active}/{decision.cap} active runner dispatches)"
                    )
            # (5) fenced marker UPDATE — commit persists the reservation. The
            # UPDATE re-checks the same fenced WHERE; a zero-row result (the
            # claim rotated mid-transaction — unreachable in production under
            # the FOR UPDATE row lock, defensive for non-transactional fakes)
            # means fenced.
            marker_result = await session.execute(
                text(
                    "UPDATE runs SET sandbox_dispatch_state = :marker "
                    "WHERE id = :rid AND organisation_id = :oid "
                    "AND claim_token = :tok AND status = 'running' "
                    "RETURNING id"
                ),
                {
                    "rid": run_id,
                    "oid": str(org_id),
                    "tok": claim_token,
                    "marker": build_dispatch_marker(attempt_key, provider),
                },
            )
            if marker_result.fetchone() is None:
                return RunnerDispatchSlot("fenced", None, False)
        return RunnerDispatchSlot("acquired", attempt_key, True)
    except RunnerCapacityDeniedError:
        raise
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        sqlstate = sqlstate_of(exc)
        if sqlstate == "55P03":
            # lock_not_available — what SET LOCAL lock_timeout actually raises.
            # Designed degradation: retryable denial + the distinct event the
            # on-call dashboard keys on.
            _log.warning(
                "runner.capacity.lock_degraded",
                extra={"run_id": run_id, "org_id": str(org_id), "node_id": node_id},
                exc_info=True,
            )
            raise RunnerCapacityDeniedError(
                f"Runner capacity lock degraded (lock_timeout={lock_timeout_ms}ms) for org {org_id}"
            ) from exc
        if sqlstate == "40P01":
            # Deadlock — expected impossible under the uniform row→advisory
            # ordering; a separate alarm, not the routine degradation path.
            _log.error(
                "runner.capacity.deadlock_degraded",
                extra={"run_id": run_id, "org_id": str(org_id), "node_id": node_id},
                exc_info=True,
            )
            raise RunnerCapacityDeniedError(f"Runner capacity gate deadlocked for org {org_id}") from exc
        # Every other DB error fails OPEN (matching the pre-D8 behaviour) —
        # the caller still writes the marker best-effort in its own tx.
        _log.warning(
            "runner.capacity.gate_error",
            extra={"run_id": run_id, "org_id": str(org_id), "node_id": node_id},
            exc_info=True,
        )
        return RunnerDispatchSlot("fail_open", None, False)


async def mark_runner_dispatch_cleared_at_hitl(
    session_factory: Any,
    *,
    org_id: uuid.UUID,
    run_id: str,
    claim_token: str | None,
) -> bool:
    """HITL-boundary tombstone (best-effort, fenced).

    Called at interrupt handling — BEFORE the run enters ``awaiting_human`` —
    so any remaining dispatch marker becomes the capacity-neutral
    ``cleared_at_hitl`` tombstone instead of surviving as a live marker on a
    parked run. Fenced on the claim token (a superseded original cannot write)
    and guarded on ``sandbox_dispatch_state IS NOT NULL`` (never resurrects a
    marker on a run that dispatched nothing). The ``script_executing`` fence
    is EXCLUDED: the tombstone must never overwrite the exactly-once fence —
    a fence-carrying marker proves a script PROCESS may have started, and
    replacing it with a capacity-neutral tombstone would let the fence be
    re-acquired (double-execute) — the sweep owns stale fences, not the
    tombstone.

    Flag-off contract (D8 rollout): the tombstone vocabulary is flag-gated —
    with ``runner_capacity_gate_enabled`` OFF this is a NO-OP (returns
    False), so the flag-off window keeps the pre-D8 behaviour exactly (the
    marker simply survives the park).
    """
    if session_factory is None or not claim_token:
        return False
    if not get_settings().runner_capacity_gate_enabled:
        return False
    from modulo.db.rls import set_rls_execution_context, set_rls_org

    try:
        async with session_factory() as session, session.begin():
            await set_rls_org(session, org_id)
            await set_rls_execution_context(session)
            result = await session.execute(
                text(
                    "UPDATE runs SET sandbox_dispatch_state = :tombstone "
                    "WHERE id = :rid AND organisation_id = :oid "
                    "AND claim_token = :tok AND status = 'running' "
                    "AND sandbox_dispatch_state IS NOT NULL "
                    'AND sandbox_dispatch_state NOT LIKE \'%"state": "script_executing"%\' '
                    "RETURNING id"
                ),
                {"rid": run_id, "oid": str(org_id), "tok": claim_token, "tombstone": build_hitl_tombstone()},
            )
            tombstoned = result.fetchone() is not None
        if tombstoned:
            _log.info(
                "runner.capacity.hitl_tombstone",
                extra={"run_id": run_id, "org_id": str(org_id)},
            )
        return tombstoned
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning(
            "runner.capacity.hitl_tombstone_failed",
            extra={"run_id": run_id, "org_id": str(org_id)},
            exc_info=True,
        )
        return False


# ---------------------------------------------------------------------------
# State-aware reconciliation sweep (D8; D1 cadence via dispatcher_reconcile)
# ---------------------------------------------------------------------------

# Teardown-failure leak / staleness threshold for non-fence markers: the
# marker's written_at (legacy tier-less markers fall back to runs.updated_at).
DEFAULT_MARKER_STALE_SECONDS = 25 * 3600

# The candidate-scan batch size (F8). A per-org scan without a LIMIT degrades
# to an unbounded row materialisation on an org with a large marker backlog;
# the sweep pages with a cursor instead. Default 500. (An env-tunable knob
# would belong in modulo.settings — left a module constant pending that.)
SWEEP_CANDIDATE_BATCH = 500

_SWEEP_CANDIDATE_SQL = text(
    "SELECT id, status, error_code, sandbox_dispatch_state, updated_at "
    "FROM runs "
    "WHERE organisation_id = :oid AND sandbox_dispatch_state IS NOT NULL "
    "AND (CAST(:after AS uuid) IS NULL OR id > CAST(:after AS uuid)) "
    "ORDER BY id "
    "LIMIT :batch"
)

# CAS sweep updates (F2): every clear/transition re-checks the EXACT marker
# text it classified (``marker_seen``). A concurrent fresh-marker commit
# between the candidate SELECT and this UPDATE makes the UPDATE match 0 rows —
# the sweep never clobbers (or terminalises over) a marker it did not
# classify, and never nulls a just-refreshed reservation.

# A clear keeps ``sandbox_id``: the run is already terminal (or parked), and
# the sandbox id is the evidence the D4 workspace reconciler needs to find and
# destroy the container. Only the stale-RUNNING transition nulls it (that run
# is killed — its workspace must not be re-adopted).
_CLEAR_MARKER_SQL = text(
    "UPDATE runs SET sandbox_dispatch_state = NULL "
    "WHERE id = :rid AND organisation_id = :oid AND sandbox_dispatch_state IS NOT NULL "
    "AND sandbox_dispatch_state = :marker_seen "
    "RETURNING id"
)

# A stale-clear on a non-terminal RUNNING run also transitions it terminal
# (the house worker-death code) so no zombie
# running-without-marker-without-workspace state can be minted. Status-guarded
# for idempotency: a run that terminated between the candidate select and this
# UPDATE is left alone. CAS-guarded on the classified marker text.
_TRANSITION_STALE_RUNNING_SQL = text(
    "UPDATE runs SET status = 'failed', error_code = 'worker_lost', "
    "error_detail = :detail, completed_at = now(), "
    "sandbox_dispatch_state = NULL, sandbox_id = NULL "
    "WHERE id = :rid AND organisation_id = :oid AND status = 'running' "
    "AND sandbox_dispatch_state IS NOT NULL "
    "AND sandbox_dispatch_state = :marker_seen "
    "RETURNING id"
)

_SWEEP_STALE_DETAIL = (
    "Runner marker reconciliation: dispatch marker stale past threshold; "
    "slot reclaimed and run terminalised (FAR-594 D8)."
)


def sweep_staleness_reference(
    marker_json: str | None,
    updated_at: datetime | None,
) -> datetime | None:
    """The timestamp the sweep's staleness rule compares (pure function).

    The marker's ``written_at`` when present; legacy tier-less markers fall
    back to ``runs.updated_at``.
    """
    return parse_marker_written_at(marker_json) or updated_at


def classify_sweep_action(
    *,
    status: str,
    error_code: str | None,
    marker_json: str | None,
    stale_reference: datetime | None,
    now: datetime,
    stale_seconds: int,
    recoverable: bool,
    row_fresh: bool = False,
) -> str:
    """The sweep's per-row decision (pure function — heavily unit-tested).

    Returns one of: ``"keep_fence"`` (a), ``"keep_recoverable"`` (b),
    ``"keep_anomaly"`` (c), ``"clear_terminal"`` (d1),
    ``"transition_stale_running"`` (d2), ``"clear_stale"`` (d3),
    ``"keep_live"`` (e). Terminal rows are handled FIRST: the anomaly-code
    exemption outranks the fence rule (the rollback evaluator owns those rows
    even when the marker carries a crash-leaked fence), and a terminal
    non-anomaly run's crash-leaked fence clears past the staleness cap (the
    longer age cap — a just-terminalised run may still be mid-teardown) while
    a terminal non-fence marker clears immediately (d1). Non-terminal rows
    then follow fence → recoverable → staleness; only a STALE non-fence
    marker is cleared, and a stale clear on a RUNNING run also transitions it
    terminal (d2) UNLESS the row itself is fresh (``row_fresh`` — e.g. a
    freshly-resumed long-parked run whose tombstone marker is old but whose
    heartbeat/updated_at is live): a fresh row is alive, so the sweep only
    clears the stale marker, never terminalises over a live attempt.
    """
    if status in TERMINAL_STATUSES:
        # Terminal rows: anomaly exemption BEFORE the fence rule (qa F14) —
        # a crash-leaked fence on a terminal non-anomaly run must not pin its
        # marker forever.
        from modulo.core.rollback_thresholds import SCRIPT_ANOMALY_ERROR_CODES

        if error_code in SCRIPT_ANOMALY_ERROR_CODES:
            return "keep_anomaly"
        if marker_is_fence_component(marker_json):
            # Crash-leaked fence on a terminal non-anomaly run: clear only
            # past the staleness cap (the longer age cap), never immediately —
            # the teardown may legitimately still be writing the lease.
            if stale_reference is None:
                return "keep_live"
            if (now - stale_reference).total_seconds() <= stale_seconds:
                return "keep_live"
            return "clear_terminal"
        return "clear_terminal"
    if marker_is_fence_component(marker_json):
        return "keep_fence"
    if recoverable:
        return "keep_recoverable"
    # Non-terminal: only a STALE non-fence marker is cleared. A stale clear on
    # a RUNNING run also transitions it terminal (no zombie) — unless the row
    # itself is FRESH (a freshly-resumed long-parked run: its tombstone /
    # previous-attempt marker is old but the heartbeat is live), where the
    # sweep clears only the marker. The other non-terminal statuses keep their
    # lifecycle (a parked/waiting run is not killed over a stale marker) — the
    # marker clear alone makes them capacity-neutral.
    if stale_reference is None:
        return "keep_live"
    age = (now - stale_reference).total_seconds()
    if age <= stale_seconds:
        return "keep_live"
    if status == "running" and not row_fresh:
        return "transition_stale_running"
    return "clear_stale"


def _sweep_recoverability_predicate() -> tuple[Any, Any]:
    """The reconciler's OWN recovery predicate set, pre-composed (parity by
    construction).

    Returns ``(recovery_or, exclusion)``: ``recovery_or`` is the SAME
    OR-composition the reconciler's scan selects with
    (``cron_helpers.reconciler_recovery_predicate`` — re-dispatch predicate OR
    the nodeless zombie branch), and ``exclusion`` is the capacity-marker
    exclusion applied AFTER the OR (exactly the scan's
    ``WHERE recovery OR … AND NOT capacity-marker-excluded`` shape). Lazy
    import: cron_helpers transitively reaches dispatch/pipeline machinery that
    runner_capacity's importers (node_runner) must not pull at module load.
    """
    from modulo.core.cron_helpers import (
        CAPACITY_REDISPATCH_SECONDS,
        ENQUEUE_FAILED_REDISPATCH_SECONDS,
        RECONCILE_STALE_HEARTBEAT_FACTOR,
        reconcile_capacity_marker_exclusion,
        reconciler_recovery_predicate,
    )

    settings = get_settings()
    stale_window = RECONCILE_STALE_HEARTBEAT_FACTOR * int(settings.saq_job_heartbeat)
    recovery_or = reconciler_recovery_predicate(
        reenqueue_window=int(settings.saq_reenqueue_window),
        stale_window=stale_window,
        capacity_redispatch_seconds=CAPACITY_REDISPATCH_SECONDS,
        nodeless_window=int(settings.saq_claimed_nodeless_minutes),
        enqueue_failed_redispatch_seconds=ENQUEUE_FAILED_REDISPATCH_SECONDS,
    )
    return recovery_or, reconcile_capacity_marker_exclusion(CAPACITY_REDISPATCH_SECONDS)


async def _run_recoverable(session: AsyncSession, run_id: Any, recovery_or: Any, exclusion: Any) -> bool:
    """True when the candidate row matches the reconciler's recovery predicate.

    The composition is the reconciler's OWN OR (``re_dispatch OR nodeless
    zombie``) with the capacity-marker exclusion applied after it — the
    identical shape of the reconcile scan's WHERE (qa F3: the previous
    AND-of-all-three evaluation misjudged a fresh-heartbeat nodeless zombie —
    matched ONLY by the nodeless branch — as unrecoverable).
    """
    from modulo.db.models.run import Run

    combined = func.count()
    stmt = select(combined).select_from(Run).where(Run.id == run_id).where(recovery_or).where(exclusion)
    result = await session.execute(stmt)
    return int(result.scalar_one() or 0) > 0


async def _assert_capacity_within_cap(session: AsyncSession, org_id: uuid.UUID) -> bool:
    """Sweep rule (h): assert the live count ≤ cap; log a violation on breach.

    Returns ``True`` when the live count exceeds the cap (a real breach) and
    ``False`` otherwise. This is the D8 ROLLBACK signal — a sustained breach
    means the atomic gate is not holding and the rollout flag must go off. A
    healthy org sitting below cap (including one with live dispatches) MUST NOT
    be reported as a breach, otherwise the dispatcher-reconcile ``violations``
    summary and the liveness key become indistinguishable from noise and a true
    breach is masked (qa F4). The caller increments the violations counter
    ONLY on ``True`` — AFTER the org transaction commits, so a rolled-back
    org pass never mints a phantom counter increment.
    """
    decision = await resolve_runner_capacity_decision(session, org_id)
    breached = decision.cap is not None and decision.active > decision.cap
    if breached:
        _log.error(
            "runner.capacity.violation",
            extra={
                "org_id": str(org_id),
                "active": decision.active,
                "cap": decision.cap,
                "host_resource_only": decision.host_resource_only,
            },
        )
    return breached


@dataclass(frozen=True)
class RunnerMarkerSweepError(RuntimeError):
    """The marker sweep failed (partially or wholly) — qa F5 liveness contract.

    Carries the PARTIAL counts the tick achieved (orgs after the failure were
    still swept in the same tick) so the SAQ cron wrapper can persist them
    with ``"error": "sweep_failed"`` before re-raising (SAQ ``retries=2``
    engages — mirroring the ``runner_workspace_reconcile`` sibling contract).
    A swallowed sweep failure is a silently dead safety net: stale markers
    would accumulate as phantom capacity and the D8 rollback signal would go
    dark without anyone noticing.
    """

    scanned: int
    cleared: int
    transitioned: int
    org_failures: int


# Bounded polling for the SESSION-scoped sweep dedup advisory lock. Mirrors
# ``api.main._migration_advisory_lock`` / ``db.migrations.env._migration_advisory_lock``:
# a bare blocking ``pg_advisory_lock`` under a client timeout races server-side
# acquisition against the client (AGENTS.md), so we poll ``pg_try_advisory_lock``
# on a DEDICATED engine connection and fail open if it never becomes free.
_SWEEP_LOCK_POLL_ATTEMPTS = 240
_SWEEP_LOCK_POLL_INTERVAL = 1.0


def _marker_sweep_lock_engine(factory: Any) -> Any:
    """The engine that owns the sweep dedup lock connection.

    Uses the session factory's bound engine (the SAME engine the sweep's org
    sessions use) so the lock lives on a sibling connection of the same pool. Real
    callers (``saq_worker._make_session_factory`` / ``cron_helpers._open_factory``)
    pass a real ``async_sessionmaker`` whose engine lives in ``factory.kw['bind']``
    (SQLAlchemy 2.0 does NOT expose ``.bind`` on ``async_sessionmaker`` — verified
    empirically against the pinned 2.0.52). ``modulo.core`` must not depend on
    ``modulo.api``, so there is deliberately no app-engine fallback."""
    engine = factory.kw.get("bind") if hasattr(factory, "kw") else None
    if engine is None:
        raise RuntimeError("reconcile_runner_dispatch_markers requires a session factory with a bound engine")
    return engine


async def _acquire_sweep_dedup_lock(factory: Any, k1: int, k2: int) -> tuple[bool, Any]:
    """Acquire the SESSION-scoped sweep dedup lock on a dedicated connection.

    The lock is held on the connection returned here for the WHOLE sweep and
    released on that SAME handle by :func:`_release_sweep_dedup_lock` — never on a
    freshly-checked-out pool connection — so a leaked lock can never hang a later
    sweep indefinitely. Uses ``pg_try_advisory_lock`` + bounded polling (the
    codebase convention); if the lock cannot be acquired the connection is closed
    and ``(False, None)`` is returned (fail-open)."""
    engine = _marker_sweep_lock_engine(factory)
    # ``engine.connect()`` sits INSIDE the try so a connection-establishment
    # failure fails open (the documented behaviour) rather than propagating out of
    # the sweep and killing the dispatcher reconcile / cron liveness write.
    try:
        lock_conn = await engine.connect()
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning("runner.capacity.marker_sweep_lock_failed", exc_info=True)
        return False, None
    try:
        for _ in range(_SWEEP_LOCK_POLL_ATTEMPTS):
            result = await lock_conn.execute(
                text("SELECT pg_try_advisory_lock(:k1, :k2)"),
                {"k1": k1, "k2": k2},
            )
            if bool(result.scalar_one()):
                return True, lock_conn
            await asyncio.sleep(_SWEEP_LOCK_POLL_INTERVAL)
        # Lock never freed within the poll budget: fail open (do not hang).
        _log.info("runner.capacity.marker_sweep_skipped_locked")
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning("runner.capacity.marker_sweep_lock_failed", exc_info=True)
    try:
        await lock_conn.close()
    except Exception:
        _log.debug("runner.capacity.marker_sweep_lock_conn_close_failed", exc_info=True)
    return False, None


async def _release_sweep_dedup_lock(lock_conn: Any, k1: int, k2: int) -> None:
    """Release the sweep dedup lock on the SAME connection that acquired it."""
    try:
        await lock_conn.execute(
            text("SELECT pg_advisory_unlock(:k1, :k2)"),
            {"k1": k1, "k2": k2},
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.debug("runner.capacity.marker_sweep_unlock_failed", exc_info=True)
    finally:
        try:
            await lock_conn.close()
        except Exception:
            _log.debug("runner.capacity.marker_sweep_lock_conn_close_failed", exc_info=True)


async def reconcile_runner_dispatch_markers(
    factory: async_sessionmaker[AsyncSession],
    *,
    stale_seconds: int | None = None,
) -> dict[str, Any]:
    """Sweep stale/terminal non-fence runner dispatch markers (D8 rule set).

    Per org (RLS-scoped transaction): every run row carrying a marker is
    classified by :func:`classify_sweep_action` — terminal runs carrying
    anomaly error codes survive (c, evaluated BEFORE the fence rule),
    genuinely terminal non-fence markers are cleared (d1) and terminal
    crash-leaked fences clear past the staleness cap, fence components on
    non-terminal rows survive (a), reconciler-recoverable rows survive (b,
    evaluated with the reconciler's OWN OR-composed predicates — LAZILY, only
    for non-terminal non-fence rows; terminal rows skip the check entirely
    since the recovery predicates are status-scoped), stale markers on
    non-terminal rows are cleared with the RUNNING case also terminalised
    unless the row is fresh (d2/d3), and live markers are untouched (e).
    Every clear/transition UPDATE is CAS-guarded on the classified marker
    text (a concurrent fresh-marker commit makes it match 0 rows), the
    candidate scan is batched with a cursor (``SWEEP_CANDIDATE_BATCH``), and
    every committed clear emits ``runner.capacity.marker_cleared`` (g) AFTER
    the org transaction commits — a rolled-back org pass emits nothing
    (no phantom coordination notes for the D4 reconciler). The live count is
    asserted against the cap per org (h); the violations counter increments
    ONLY on a genuine breach (a cap-less org never violates).

    A sweep tick takes the DEDUP advisory lock (distinct derivation suffix)
    SESSION-scoped on a DEDICATED ``engine.connect()`` connection held for the
    sweep's lifetime — the lock is acquired with ``pg_try_advisory_lock`` +
    bounded polling and released on that SAME connection (not a freshly-checked
    out pool connection) in the sweep's finally. Belt-and-braces against
    double-sweep (the cron cadence AND the 60s dispatcher_reconcile path can
    overlap — SAQ's ``unique=True`` dedupes only the cron job itself) on top of
    the per-row CAS guards.

    Returns ``{"scanned", "cleared", "transitioned", "violations",
    "orgs_failed"}``. Raises :class:`RunnerMarkerSweepError` when the org
    index fails or any org pass fails (carrying the partial counts).
    """
    settings = get_settings()
    stale_window = stale_seconds if stale_seconds is not None else settings.runner_marker_stale_seconds
    # The "fresh row" window for the d2 exemption (qa F14): a run whose row
    # was written more recently than the reconciler's stale-heartbeat window
    # is alive (heartbeat writes refresh updated_at) — the sweep clears its
    # stale marker but never terminalises over it.
    from modulo.core.cron_helpers import RECONCILE_STALE_HEARTBEAT_FACTOR

    fresh_window = RECONCILE_STALE_HEARTBEAT_FACTOR * int(settings.saq_job_heartbeat)
    now = datetime.now(UTC)
    scanned = 0
    cleared = 0
    transitioned = 0
    violations = 0
    orgs_failed = 0

    k1, k2 = runner_marker_sweep_lock_keys()
    # Hold the SESSION-scoped dedup lock on a DEDICATED engine connection for the
    # sweep's lifetime. ``pg_try_advisory_lock`` + bounded polling (the codebase
    # convention) serialises two contending sweeps instead of letting one skip
    # the tick; the lock is released on the SAME connection in the finally below.
    acquired, lock_conn = await _acquire_sweep_dedup_lock(factory, k1, k2)
    if not acquired:
        # Fail-open: NEVER skip the safety net. A skipped sweep lets stale
        # terminal/leaked markers accumulate as phantom capacity (the D8
        # rollback signal goes dark) — the worse failure mode than a
        # concurrent re-process, which the per-row CAS guards make
        # idempotent. So proceed without the dedup lock rather than
        # returning early; the documented overlap guards hold.
        _log.warning("runner.capacity.marker_sweep_lock_not_acquired_proceeding")

    try:
        recovery_or, exclusion = _sweep_recoverability_predicate()

        try:
            async with factory() as org_index_session:
                org_result = await org_index_session.execute(text("SELECT id FROM organisations"))
                org_ids: list[uuid.UUID] = [row[0] for row in org_result.all()]
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("runner.capacity.marker_sweep_org_index_failed")
            raise RunnerMarkerSweepError(
                scanned=scanned, cleared=cleared, transitioned=transitioned, org_failures=orgs_failed
            ) from None

        for org_id in org_ids:
            # Outcomes decided inside the org transaction, emitted AFTER its
            # commit (qa F14 phantom-event fix): a rolled-back org pass must
            # not leave ``runner.capacity.marker_cleared`` notes or counters
            # describing writes that never committed.
            committed_outcomes: list[tuple[uuid.UUID, str, str]] = []
            org_breach = False
            try:
                async with factory() as session, session.begin():
                    from modulo.db.rls import set_rls_org

                    await set_rls_org(session, org_id)
                    cursor: uuid.UUID | None = None
                    while True:
                        batch = (
                            await session.execute(
                                _SWEEP_CANDIDATE_SQL,
                                {"oid": str(org_id), "after": cursor, "batch": SWEEP_CANDIDATE_BATCH},
                            )
                        ).all()
                        if not batch:
                            break
                        for row in batch:
                            scanned += 1
                            marker_json = row.sandbox_dispatch_state
                            is_terminal = row.status in TERMINAL_STATUSES
                            # Lazy recoverability (F8): terminal rows skip the
                            # check entirely (the recovery predicates are
                            # status-scoped — they can never match a terminal
                            # row); fence rows skip it too (rule (a) returns
                            # before the recoverability branch would be read).
                            if is_terminal or marker_is_fence_component(marker_json):
                                recoverable = False
                            else:
                                recoverable = await _run_recoverable(session, row.id, recovery_or, exclusion)
                            row_fresh = row.updated_at is not None and (
                                (now - row.updated_at).total_seconds() <= fresh_window
                            )
                            action = classify_sweep_action(
                                status=row.status,
                                error_code=row.error_code,
                                marker_json=marker_json,
                                stale_reference=sweep_staleness_reference(marker_json, row.updated_at),
                                now=now,
                                stale_seconds=stale_window,
                                recoverable=recoverable,
                                row_fresh=row_fresh,
                            )
                            if action in ("keep_fence", "keep_recoverable", "keep_anomaly", "keep_live"):
                                continue
                            if action in ("clear_terminal", "clear_stale"):
                                result = await session.execute(
                                    _CLEAR_MARKER_SQL,
                                    {"rid": row.id, "oid": str(org_id), "marker_seen": marker_json},
                                )
                                if result.fetchone() is not None:
                                    committed_outcomes.append((row.id, row.status, action))
                            elif action == "transition_stale_running":
                                result = await session.execute(
                                    _TRANSITION_STALE_RUNNING_SQL,
                                    {
                                        "rid": row.id,
                                        "oid": str(org_id),
                                        "detail": _SWEEP_STALE_DETAIL,
                                        "marker_seen": marker_json,
                                    },
                                )
                                if result.fetchone() is not None:
                                    committed_outcomes.append((row.id, row.status, "transition_stale_running"))
                        cursor = batch[-1].id
                        if len(batch) < SWEEP_CANDIDATE_BATCH:
                            break
                    org_breach = await _assert_capacity_within_cap(session, org_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                orgs_failed += 1
                _log.exception("runner.capacity.marker_sweep_org_failed org=%s", org_id)
                continue
            # The org transaction COMMITTED — emit the outcomes + the breach
            # verdict now (post-commit, never phantom).
            for run_id, run_status, reason in committed_outcomes:
                cleared += 1
                if reason == "transition_stale_running":
                    transitioned += 1
                _log.warning(
                    "runner.capacity.marker_cleared",
                    extra={
                        "run_id": str(run_id),
                        "org_id": str(org_id),
                        "run_status": run_status,
                        "reason": reason,
                        "note": "container destroy owned by the D4 reconciler",
                    },
                )
            if org_breach:
                violations += 1
        _log.info(
            "runner.capacity.marker_swept scanned=%d cleared=%d transitioned=%d",
            scanned,
            cleared,
            transitioned,
        )
        if orgs_failed:
            raise RunnerMarkerSweepError(
                scanned=scanned, cleared=cleared, transitioned=transitioned, org_failures=orgs_failed
            )
        return {
            "scanned": scanned,
            "cleared": cleared,
            "transitioned": transitioned,
            "violations": violations,
            "orgs_failed": 0,
        }
    finally:
        # Release the SESSION-scoped dedup lock on the SAME dedicated connection
        # that acquired it (never a freshly-checked-out pool connection) so a
        # leaked lock can never hang a later sweep indefinitely. The
        # ``pg_try_advisory_lock`` + bounded-polling convention (above) means the
        # lock only engages when free, and this release is on the acquiring
        # handle — closing the connection also releases it as a backstop if the
        # explicit unlock is skipped (fail-open) or the connection dies.
        if acquired and lock_conn is not None:
            await _release_sweep_dedup_lock(lock_conn, k1, k2)


__all__ = [
    "HOST_RESOURCE_PROVIDERS",
    "MARKER_STATE_CLEARED_AT_HITL",
    "MARKER_STATE_DISPATCHING",
    "MARKER_STATE_SCRIPT_EXECUTING",
    "RUNNER_PROVIDER_DOCKER",
    "RUNNER_PROVIDER_E2B",
    "RUNNER_PROVIDER_LOCAL",
    "RunnerCapacityDecision",
    "RunnerCapacityDeniedError",
    "RunnerDispatchSlot",
    "RunnerMarkerSweepError",
    "acquire_runner_dispatch_slot",
    "build_dispatch_marker",
    "build_hitl_tombstone",
    "classify_sweep_action",
    "mark_runner_dispatch_cleared_at_hitl",
    "marker_is_fence_component",
    "parse_marker_provider",
    "parse_marker_state",
    "parse_marker_written_at",
    "read_runner_cap_contract",
    "reconcile_runner_dispatch_markers",
    "resolve_runner_capacity_decision",
    "runner_capacity_lock_keys",
    "runner_marker_sweep_lock_keys",
    "sweep_staleness_reference",
]
