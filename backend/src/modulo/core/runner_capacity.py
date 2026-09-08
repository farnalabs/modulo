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
  signal).
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
against a per-org gate key.

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
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
# default-cap bucket must not undercount). Compile-time constants only —
# nothing user-controlled is interpolated.
_RUNNER_PROVIDER_SQL = (
    "COALESCE("
    "CASE WHEN runs.sandbox_dispatch_state LIKE '%\"provider\"%' "
    "THEN substring(runs.sandbox_dispatch_state from "
    '\'"provider"[[:space:]]*:[[:space:]]*"([a-z_]+)"\') END, '
    "'runner_docker')"
)
# Tombstoned markers (cleared at the HITL boundary) hold no slot.
_RUNNER_TOMBSTONE_EXCLUSION_SQL = "runs.sandbox_dispatch_state NOT LIKE '%cleared_at_hitl%'"


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

    ``cap is None`` = no gate. ``host_resource_only`` marks the Docker-tier
    default scope (the count must filter to ``HOST_RESOURCE_PROVIDERS``);
    legacy tier-less markers count into that bucket via the SQL attribution.
    """

    cap: int | None
    active: int
    host_resource_only: bool
    flag_on: bool


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

    Used by the lock-free paths (HITL pre-check, claim-time read) and by the
    gate AFTER the advisory lock. The count population is ALWAYS the narrowed
    D8 one (``running`` runs with a live marker, own marker excluded) — the
    only status a marker can legally exist on.
    """
    cap, host_resource_only = await read_runner_cap_contract(session, org_id)
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
        flag_on=get_settings().runner_capacity_gate_enabled,
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

    status: str
    attempt_key: str | None
    marker_set: bool


def _sqlstate(exc: BaseException) -> str | None:
    """Best-effort SQLSTATE from a SQLAlchemy DBAPIError wrapper."""
    orig = getattr(exc, "orig", None)
    return getattr(orig, "sqlstate", None)


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
    write still runs so a fail-open dispatch is never markerless.
    """
    if session_factory is None or not claim_token:
        # Fail-open: no claim context — the legacy fail-open marker path in
        # the caller handles both the attempt key and the best-effort write.
        return RunnerDispatchSlot("fail_open", None, False)
    settings = get_settings()
    lock_timeout_ms = settings.runner_capacity_lock_timeout_ms
    flag_on = settings.runner_capacity_gate_enabled
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
                # marker cannot self-block).
                decision = await resolve_runner_capacity_decision(session, org_id, exclude_run_id=run_uuid)
            else:
                # Flag-off: the pre-D8 RACY check-then-act decision (no
                # advisory lock, no tier scoping) over the new unified
                # running-only population — exactly the flag-off enumeration.
                cap, host_resource_only = await read_runner_cap_contract(session, org_id)
                active = (
                    await count_active_runner_dispatches_for_decision(
                        session, org_id, exclude_run_id=run_uuid, host_resource_only=host_resource_only
                    )
                    if cap is not None
                    else 0
                )
                decision = RunnerCapacityDecision(
                    cap=cap,
                    active=active,
                    host_resource_only=host_resource_only,
                    flag_on=False,
                )
            if decision.cap is not None and decision.active >= decision.cap:
                # The Docker-tier default (absent key) gates ONLY host-resource
                # dispatches: an e2b dispatch is neither counted into that
                # bucket nor denied by it (e2b carries its own platform-side
                # concurrency quota). Legacy tier-less dispatchers attribute
                # to Docker and stay gated.
                docker_default_skips_e2b = decision.host_resource_only and provider == RUNNER_PROVIDER_E2B
                if not docker_default_skips_e2b:
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
        sqlstate = _sqlstate(exc)
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
    marker on a run that dispatched nothing).
    """
    if session_factory is None or not claim_token:
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

_SWEEP_CANDIDATE_SQL = text(
    "SELECT id, status, error_code, sandbox_dispatch_state, updated_at "
    "FROM runs "
    "WHERE organisation_id = :oid AND sandbox_dispatch_state IS NOT NULL"
)

_CLEAR_MARKER_SQL = text(
    "UPDATE runs SET sandbox_dispatch_state = NULL, sandbox_id = NULL "
    "WHERE id = :rid AND organisation_id = :oid AND sandbox_dispatch_state IS NOT NULL "
    "RETURNING id"
)

# A stale-clear on a non-terminal RUNNING run also transitions it terminal
# (the house worker-death code) so no zombie
# running-without-marker-without-workspace state can be minted. Status-guarded
# for idempotency: a run that terminated between the candidate select and this
# UPDATE is left alone.
_TRANSITION_STALE_RUNNING_SQL = text(
    "UPDATE runs SET status = 'failed', error_code = 'worker_lost', "
    "error_detail = :detail, completed_at = now(), "
    "sandbox_dispatch_state = NULL, sandbox_id = NULL "
    "WHERE id = :rid AND organisation_id = :oid AND status = 'running' "
    "AND sandbox_dispatch_state IS NOT NULL "
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
) -> str:
    """The sweep's per-row decision (pure function — heavily unit-tested).

    Returns one of: ``"keep_fence"`` (a), ``"keep_recoverable"`` (b),
    ``"keep_anomaly"`` (c), ``"clear_terminal"`` (d1),
    ``"transition_stale_running"`` (d2), ``"clear_stale"`` (d3),
    ``"keep_live"`` (e). Fence precedence outranks staleness; recoverability
    and the anomaly-code exemption outrank both (the reconciler and the
    rollback evaluator own those rows).
    """
    if marker_is_fence_component(marker_json):
        return "keep_fence"
    if recoverable:
        return "keep_recoverable"
    if status not in ("running", "awaiting_human", "claimed", "hitl_parked", "pending"):
        # Genuinely terminal: a non-fence marker is cleared unless the run
        # carries an anomaly error code (rollback_thresholds
        # ._count_claim_without_marker requires those markers).
        from modulo.core.rollback_thresholds import _SCRIPT_ANOMALY_ERROR_CODES

        if error_code in _SCRIPT_ANOMALY_ERROR_CODES:
            return "keep_anomaly"
        return "clear_terminal"
    # Non-terminal: only a STALE non-fence marker is cleared. A stale clear on
    # a RUNNING run also transitions it terminal (no zombie); the other
    # non-terminal statuses keep their lifecycle (a parked/waiting run is not
    # killed over a stale marker) — the marker clear alone makes them
    # capacity-neutral.
    if stale_reference is None:
        return "keep_live"
    age = (now - stale_reference).total_seconds()
    if age <= stale_seconds:
        return "keep_live"
    return "transition_stale_running" if status == "running" else "clear_stale"


def _sweep_recoverability_predicate() -> Any:
    """The reconciler's OWN re-dispatch predicate set (parity by construction).

    The D8 sweep must never clear a marker on a run ``dispatcher_reconcile``
    considers recoverable. Rather than re-implementing the multi-branch rule
    set (silent-drift invitation), it evaluates the SAME predicate objects the
    reconciler selects with — ``_build_re_dispatch_predicate`` + the nodeless
    zombie branch + the capacity-marker exclusion — against each candidate
    row. Lazy import: cron_helpers transitively reaches dispatch/pipeline
    machinery that runner_capacity's importers (node_runner) must not pull at
    module load.
    """
    from modulo.core.cron_helpers import (
        CAPACITY_REDISPATCH_SECONDS,
        ENQUEUE_FAILED_REDISPATCH_SECONDS,
        RECONCILE_STALE_HEARTBEAT_FACTOR,
        _build_re_dispatch_predicate,
        _nodeless_zombie_predicate,
        _reconcile_capacity_marker_exclusion,
    )

    settings = get_settings()
    stale_window = RECONCILE_STALE_HEARTBEAT_FACTOR * int(settings.saq_job_heartbeat)
    return (
        _build_re_dispatch_predicate(
            reenqueue_window=int(settings.saq_reenqueue_window),
            stale_window=stale_window,
            capacity_redispatch_seconds=CAPACITY_REDISPATCH_SECONDS,
            enqueue_failed_redispatch_seconds=ENQUEUE_FAILED_REDISPATCH_SECONDS,
        ),
        _nodeless_zombie_predicate(int(settings.saq_claimed_nodeless_minutes)),
        _reconcile_capacity_marker_exclusion(CAPACITY_REDISPATCH_SECONDS),
    )


async def _run_recoverable(session: AsyncSession, run_id: Any, predicates: tuple[Any, ...]) -> bool:
    """True when the candidate row matches ANY reconciler recovery predicate."""
    from modulo.db.models.run import Run

    combined = func.count()
    stmt = select(combined).select_from(Run).where(Run.id == run_id)
    for predicate in predicates:
        stmt = stmt.where(predicate)
    result = await session.execute(stmt)
    return int(result.scalar_one() or 0) > 0


async def _assert_capacity_within_cap(session: AsyncSession, org_id: uuid.UUID) -> int:
    """Sweep rule (h): assert the live count ≤ cap; log a violation otherwise.

    Returns the live count. This is the D8 ROLLBACK signal — a sustained
    breach means the atomic gate is not holding and the rollout flag must go
    off.
    """
    decision = await resolve_runner_capacity_decision(session, org_id)
    if decision.cap is not None and decision.active > decision.cap:
        _log.error(
            "runner.capacity.violation",
            extra={
                "org_id": str(org_id),
                "active": decision.active,
                "cap": decision.cap,
                "host_resource_only": decision.host_resource_only,
            },
        )
    return decision.active


async def reconcile_runner_dispatch_markers(
    factory: async_sessionmaker[AsyncSession],
    *,
    stale_seconds: int | None = None,
) -> dict[str, Any]:
    """Sweep stale/terminal non-fence runner dispatch markers (D8 rule set).

    Per org (RLS-scoped transaction): every run row carrying a marker is
    classified by :func:`classify_sweep_action` — fence components survive
    (a), reconciler-recoverable rows survive (b, evaluated with the
    reconciler's OWN predicates), terminal runs carrying anomaly error codes
    survive (c), genuinely terminal non-fence markers are cleared (d1), stale
    markers on non-terminal rows are cleared with the RUNNING case also
    terminalised (d2/d3), and live markers are untouched (e). Every clear
    emits ``runner.capacity.marker_cleared`` (g) — the coordination note D4's
    container reconciler can consume (the cleared run is terminal or
    capacity-neutral, so its workspace becomes an orphan the D4 reconciler
    owns destroying). After each org's pass the live count is asserted ≤ cap
    with ``runner.capacity.violation`` on breach (h — the rollback signal).

    A sweep tick takes the DEDUP advisory lock (distinct derivation suffix)
    in its own short transaction — belt-and-braces against double-sweep on
    top of SAQ's ``unique=True``.

    Returns ``{"scanned", "cleared", "transitioned", "violations"}``.
    """
    settings = get_settings()
    stale_window = stale_seconds if stale_seconds is not None else settings.runner_marker_stale_seconds
    now = datetime.now(UTC)
    scanned = 0
    cleared = 0
    transitioned = 0
    violations = 0

    k1, k2 = runner_marker_sweep_lock_keys()
    try:
        async with factory() as lock_session, lock_session.begin():
            acquired = (
                await lock_session.execute(
                    text("SELECT pg_try_advisory_xact_lock(:k1, :k2)"),
                    {"k1": k1, "k2": k2},
                )
            ).scalar_one()
        if not acquired:
            _log.info("runner.capacity.marker_sweep_skipped_locked")
            return {"scanned": 0, "cleared": 0, "transitioned": 0, "violations": 0}
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning("runner.capacity.marker_sweep_lock_failed", exc_info=True)
        # Fail-open: the sweep proceeds; SAQ unique=True remains the overlap guard.

    predicates = _sweep_recoverability_predicate()

    try:
        async with factory() as org_index_session:
            org_result = await org_index_session.execute(text("SELECT id FROM organisations"))
            org_ids: list[uuid.UUID] = [row[0] for row in org_result.all()]
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("runner.capacity.marker_sweep_org_index_failed")
        return {"scanned": 0, "cleared": 0, "transitioned": 0, "violations": 0}

    for org_id in org_ids:
        try:
            async with factory() as session, session.begin():
                from modulo.db.rls import set_rls_org

                await set_rls_org(session, org_id)
                candidates = (await session.execute(_SWEEP_CANDIDATE_SQL, {"oid": str(org_id)})).all()
                for row in candidates:
                    scanned += 1
                    action = classify_sweep_action(
                        status=row.status,
                        error_code=row.error_code,
                        marker_json=row.sandbox_dispatch_state,
                        stale_reference=sweep_staleness_reference(row.sandbox_dispatch_state, row.updated_at),
                        now=now,
                        stale_seconds=stale_window,
                        recoverable=await _run_recoverable(session, row.id, predicates),
                    )
                    if action in ("keep_fence", "keep_recoverable", "keep_anomaly", "keep_live"):
                        continue
                    if action in ("clear_terminal", "clear_stale"):
                        result = await session.execute(_CLEAR_MARKER_SQL, {"rid": row.id, "oid": str(org_id)})
                        if result.fetchone() is not None:
                            cleared += 1
                            _log.info(
                                "runner.capacity.marker_cleared",
                                extra={
                                    "run_id": str(row.id),
                                    "org_id": str(org_id),
                                    "run_status": row.status,
                                    "reason": action,
                                    "note": "container destroy owned by the D4 reconciler",
                                },
                            )
                    elif action == "transition_stale_running":
                        result = await session.execute(
                            _TRANSITION_STALE_RUNNING_SQL,
                            {"rid": row.id, "oid": str(org_id), "detail": _SWEEP_STALE_DETAIL},
                        )
                        if result.fetchone() is not None:
                            cleared += 1
                            transitioned += 1
                            _log.warning(
                                "runner.capacity.marker_cleared",
                                extra={
                                    "run_id": str(row.id),
                                    "org_id": str(org_id),
                                    "run_status": row.status,
                                    "reason": "transition_stale_running",
                                    "note": "container destroy owned by the D4 reconciler",
                                },
                            )
                if await _assert_capacity_within_cap(session, org_id) > 0:
                    violations += 1
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("runner.capacity.marker_sweep_org_failed org=%s", org_id)
    _log.info(
        "runner.capacity.marker_swept scanned=%d cleared=%d transitioned=%d",
        scanned,
        cleared,
        transitioned,
    )
    return {"scanned": scanned, "cleared": cleared, "transitioned": transitioned, "violations": violations}


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
