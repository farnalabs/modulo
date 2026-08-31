"""Evidence & no-op detection for the agent-failure UX (FAR-152).

Implements §15.3 (Evidence & no-op detection) and the §15.14 evidence-metric
subset of the agent-failure-ux-proposal:

- ``EvidenceResult`` — tri-state: ``has_work`` | ``verified_empty`` |
  ``unverifiable``. ``unverifiable`` NEVER fires a flag, logs
  ``heuristic.unverifiable``, and renders a muted "work could not be verified"
  notice — it is not evidence-empty and not evidence-has-work (§7.2).
- ``EvidenceProvider`` protocol — ``git_diff_empty(run_id, node_id)`` +
  ``sandbox_filesystem_probe(run_id, node_id)``. Connector auth probing is
  explicitly OUT of scope for v1 (§15.3).
- ``SandboxEvidenceProvider`` — concrete probe over the run's E2B sandbox.
  timeout / any exception / no-repo / no-sandbox all map to ``unverifiable``
  (never a flag). The seam is fully injectable (``sandbox_id_resolver``,
  ``run_command``, ``list_files``, ``output_json_loader``) so a
  FakeEvidenceProvider or a local tiny-git-repo double can drive
  consumer-pipeline tests.
- ``run_evidence_probe`` — the bounded (≤3s) async probe runner: probes,
  combines the two results (any positive -> has_work; any unverifiable ->
  unverifiable; else verified_empty), persists a ``run_evidence`` row, and
  emits the §15.14 metrics. Runs POST-commit, off the run's critical path.
- ``reconcile_noop_evidence`` — the bounded one-shot reconciliation sweep that
  backfills evidence rows for complete runs that missed the async window
  (crash between terminalize and probe write, §13.3/§15.3).

The probe is gated to nodes that DECLARED ``outcome: "success"`` — the only
shape where the ``agent.no_op`` flag can fire. Everything else skips the probe
entirely (§13.3, saving cost).
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo._types import _DICT_STR_ANY
from modulo.utils.uuid import coerce_uuid

_log = logging.getLogger(__name__)

#: Bounded async probe budget — code-constant, ≤3s per probe (§15.13).
EVIDENCE_PROBE_TIMEOUT_SECONDS: float = 3.0

#: Run statuses eligible for the reconciliation sweep. Only genuinely
#: terminalized ``complete`` runs (not all terminal statuses) are no-op
#: eligible — a failed/cancelled run is already a declared failure (§13.3).
#: Kept as a set so the sweep filter reads through the shared status-enum
#: convention instead of a raw ``status == "complete"`` comparison.
RECONCILE_COMPLETE_STATUSES: tuple[str, ...] = ("complete",)

#: Keys inside a node's ``output_json`` that the harness itself may stamp and
#: which therefore do NOT count as agent-produced content. The agent contract
#: keeps its base fields OUTSIDE ``output_json`` (free-form extension), so an
#: agent's ``output_json`` carrying any OTHER key with a non-empty value is
#: agent work.
_METADATA_OUTPUT_JSON_KEYS: frozenset[str] = frozenset()

#: Default per-SDK-call bound for the E2B probes (defense-in-depth under the
#: overall ≤3s probe bound).
_SANDBOX_IO_TIMEOUT_SECONDS: float = 5.0

#: E2B sandbox hourly rate used to estimate probe cost (mirrors
#: ``node_runner._E2B_SANDBOX_USD_PER_HOUR``). The probe only runs shell
#: commands / file listings against a live (retained) sandbox, so its marginal
#: cost is the sandbox wall-clock the probe itself occupies — there is no LLM
#: cost (the probe executes no model calls).
_PROBE_SANDBOX_USD_PER_HOUR: float = 0.13


class EvidenceResult(StrEnum):
    """Tri-state evidence verdict (§7.2/§15.3).

    ``unverifiable`` NEVER fires a flag: it is not evidence-empty and not
    evidence-has-work, and downstream renders a muted notice instead.
    """

    has_work = "has_work"
    verified_empty = "verified_empty"
    unverifiable = "unverifiable"


@dataclass(frozen=True)
class CommandResult:
    """A shell command result from a sandbox probe."""

    exit_code: int
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class FileInfo:
    """A sandbox filesystem entry."""

    name: str
    size: int = 0
    is_dir: bool = False


class EvidenceProvider(Protocol):
    """The evidence seam (§14.14) — injectable so consumer-pipeline tests can
    substitute a fake. Every method is async and bounded by the caller.

    Mapping documented here: timeout, any exception, and no-repo/no-sandbox all
    map to ``EvidenceResult.unverifiable`` (never flags, §14.12).
    """

    async def git_diff_empty(self, run_id: UUID, node_id: str) -> EvidenceResult: ...

    async def sandbox_filesystem_probe(self, run_id: UUID, node_id: str) -> EvidenceResult: ...


# --- §15.14 metrics (evidence subset) ----------------------------------------
# Module-level handles, lazy-init against the OTel meter provider (house
# pattern — ``modulo.core.cost_controller.breakdown.metrics``). Every record
# function is a silent no-op when no meter provider is wired, so the probe
# path can never break because telemetry is absent.

_heuristic_errors_total: Any = None
_heuristic_unverifiable_total: Any = None
_heuristic_probe_latency: Any = None
_heuristic_probe_cost: Any = None


def _get_meter() -> Any:
    try:
        from opentelemetry import metrics

        provider = metrics.get_meter_provider()
        if provider is None:
            return None
        return provider.get_meter("modulo.pipeline_engine", version="0.1.0")
    except Exception:
        return None


def _ensure() -> None:
    global _heuristic_errors_total, _heuristic_unverifiable_total, _heuristic_probe_latency, _heuristic_probe_cost
    if _heuristic_errors_total is not None:
        return
    meter = _get_meter()
    if meter is None:
        return
    _heuristic_errors_total = meter.create_counter(
        name="modulo_heuristic_errors_total",
        description="Evidence probe raised an error, by reason",
        unit="1",
    )
    _heuristic_unverifiable_total = meter.create_counter(
        name="modulo_heuristic_unverifiable_total",
        description="Evidence probes that landed unverifiable, by reason",
        unit="1",
    )
    _heuristic_probe_latency = meter.create_histogram(
        name="modulo_heuristic_probe_latency",
        description="Evidence probe duration (bounded ≤3s)",
        unit="s",
    )
    _heuristic_probe_cost = meter.create_gauge(
        name="modulo_heuristic_probe_cost",
        description="Estimated sandbox cost of the last evidence probe, USD",
        unit="USD",
    )


def record_heuristic_error(reason: str) -> None:
    if _heuristic_errors_total is None:
        _ensure()
    if _heuristic_errors_total is not None:
        _heuristic_errors_total.add(1, attributes={"reason": reason})


def record_heuristic_unverifiable(reason: str) -> None:
    if _heuristic_unverifiable_total is None:
        _ensure()
    if _heuristic_unverifiable_total is not None:
        _heuristic_unverifiable_total.add(1, attributes={"reason": reason})


def record_heuristic_probe_latency(seconds: float) -> None:
    if _heuristic_probe_latency is None:
        _ensure()
    if _heuristic_probe_latency is not None:
        _heuristic_probe_latency.record(seconds)


def record_heuristic_probe_cost(usd: float) -> None:
    if _heuristic_probe_cost is None:
        _ensure()
    if _heuristic_probe_cost is not None:
        _heuristic_probe_cost.set(usd)


# --- rollout gate -----------------------------------------------------------


def evidence_enabled() -> bool:
    """Rollout gate for the evidence/no-op machinery (§15.13). Default ON.

    Env override ``MODULO_HEURISTIC_ENABLED`` wins (manual rollout knob);
    otherwise falls back to ``settings.modulo_heuristic_enabled`` when the
    field exists, else True. Consumed via ``getattr`` because settings.py is
    outside this work's file scope — the field can ship in a follow-up without
    a code change here.
    """
    env = os.environ.get("MODULO_HEURISTIC_ENABLED")
    if env is not None:
        return env.strip().lower() in {"1", "true", "yes", "on"}
    try:
        from modulo.settings import get_settings

        return bool(getattr(get_settings(), "modulo_heuristic_enabled", True))
    except Exception:
        return True


# --- output_json helpers ----------------------------------------------------


def output_json_has_content(output_json: Any) -> bool:
    """True when output_json carries at least one non-metadata key with a
    non-empty value. Empty dict/list/string/None values do NOT count (§7.2.2).
    """
    if not isinstance(output_json, dict):
        return False
    for key, value in output_json.items():
        if key in _METADATA_OUTPUT_JSON_KEYS:
            continue
        if value is None:
            continue
        if isinstance(value, (str, list, dict)) and not value:
            continue
        return True
    return False


def extract_output_json(node_output: Any) -> dict[str, Any] | None:
    """The agent's output_json from a CAPTURED node output (the
    ``{"artifacts": [...], "output": {...}}`` envelope). None when absent.
    """
    if not isinstance(node_output, dict):
        return None
    artifacts = node_output.get("artifacts")
    if isinstance(artifacts, list) and artifacts and isinstance(artifacts[0], dict):
        inner = artifacts[0].get("output")
        if isinstance(inner, dict):
            output_json = inner.get("output_json")
            if isinstance(output_json, dict):
                return cast(_DICT_STR_ANY, output_json)
    out = node_output.get("output")
    if isinstance(out, dict):
        output_json = out.get("output_json")
        if isinstance(output_json, dict):
            return cast(_DICT_STR_ANY, output_json)
    return None


def extract_stored_output_json(
    outputs_json: Any,
    telemetry_json: Any,
    node_id: str,
) -> dict[str, Any] | None:
    """The agent's output_json for a node from the STORED run columns
    (legacy-safe). P1 (split) rows carry the pure return — for sandbox_agent
    that IS output_json; legacy rows bury it at ``artifacts[0].output.output_json``.
    """
    from modulo.core.node_output_split import node_return

    value = node_return(outputs_json, telemetry_json, node_id)
    if not isinstance(value, dict):
        return None
    artifacts = value.get("artifacts")
    if isinstance(artifacts, list) and artifacts and isinstance(artifacts[0], dict):
        inner = artifacts[0].get("output")
        if isinstance(inner, dict):
            output_json = inner.get("output_json")
            if isinstance(output_json, dict):
                return cast(_DICT_STR_ANY, output_json)
    if "artifacts" not in value and "output" not in value:
        return value
    return None


# --- no-op gate input -------------------------------------------------------


def _inner_declared_success(inner: Any) -> bool:
    """§7.2.3 gate on the INNER output dict (the split telemetry / legacy
    inner output): the agent status is present AND completed, and the outcome
    is explicitly ``success``. Missing status (legacy/unknown) never qualifies
    — where the flag can't fire anyway, saving cost (§13.3).
    """
    if not isinstance(inner, dict):
        return False
    if inner.get("agent_status") != "completed":
        return False
    return inner.get("agent_outcome") == "success"


def node_declared_success(node_output: Any) -> bool:
    """The §7.2.3 no-op gate input for a CAPTURED node output (the
    ``{"output": inner}`` envelope returned by node_runner).
    """
    if not isinstance(node_output, dict):
        return False
    return _inner_declared_success(node_output.get("output"))


def _declared_success_nodes(outputs_json: Any, telemetry_json: Any) -> list[str]:
    """Node ids whose STORED output declares success — the reconciliation
    sweep's eligibility scan (legacy-safe via node_output_split.node_telemetry).
    """
    from modulo.core.node_output_split import node_telemetry

    node_ids: set[str] = set()
    if isinstance(outputs_json, dict):
        node_ids.update(str(k) for k in outputs_json)
    if isinstance(telemetry_json, dict):
        node_ids.update(str(k) for k in telemetry_json)
    return [nid for nid in node_ids if _inner_declared_success(node_telemetry(telemetry_json, outputs_json, nid))]


# --- work_intact (terminalization, NOT the async probe) ---------------------


def _node_output_has_valid_artifact(node_output: Any) -> bool:
    """A completed node's output carries a valid artifact: a dict envelope with
    an ``artifacts[0].output`` dict, an ``output`` dict, or a summary string.
    None/strings/empty dicts are NOT valid artifacts.
    """
    if not isinstance(node_output, dict) or not node_output:
        return False
    artifacts = node_output.get("artifacts")
    if isinstance(artifacts, list) and artifacts:
        return any(isinstance(a, dict) and isinstance(a.get("output"), dict) for a in artifacts)
    return isinstance(node_output.get("output"), dict) or bool(node_output.get("summary"))


def compute_work_intact(completed_node_outputs: dict[str, Any], node_ids: set[str]) -> bool:
    """work_intact per §15.3/§2.3.2 — computed at terminalization from
    completed-node artifacts, NOT from the async evidence probe.

    True iff EVERY completed node has a valid artifact AND the completed set
    equals the full DAG's node set. A run truncated at node 3 of 5 (or with a
    completed node that produced no artifact) is NOT work-intact. Conservative
    by design: condition-skipped nodes make this False, which is the safe
    direction (never a false false-failure banner).
    """
    completed = {
        node_id for node_id, output in (completed_node_outputs or {}).items() if _node_output_has_valid_artifact(output)
    }
    if not completed:
        return False
    return completed == set(node_ids)


# --- evidence persistence ---------------------------------------------------


async def write_evidence_row(
    session: AsyncSession,
    *,
    run_id: UUID,
    node_id: str,
    evidence_state: str,
    evidence_detail: str | None,
    organisation_id: UUID | None = None,
) -> None:
    """Persist one run_evidence row.

    ``organisation_id`` is the tenant anchor required by the ``run_evidence``
    NOT NULL FK -> ``organisations`` and its org-scoped RLS policy (migration
    0133). Callers that already hold the parent run's org (the probe, the sweep)
    SHOULD pass it; when omitted it is resolved from the parent run.

    When the parent run cannot be resolved (orphaned, or already purged) the
    write is SKIPPED rather than anchored to a synthesised tenant. A fabricated
    org can never persist on the deployment target: ``organisation_id`` is a FK
    to ``organisations(id)`` and ``run_id`` a FK to ``runs(id)``, so the INSERT
    is rejected by the FK — and the 0133 ``rls_org_isolation`` WITH CHECK
    rejection is NOT an ``IntegrityError`` subclass, so it would escape the
    duplicate-key guard below and break the reconciliation sweep. An
    unresolvable parent is therefore treated as unverifiable and left unwritten
    (logged), which is the same fail-open direction as a failed probe.

    ``UNIQUE(run_id, node_id)`` is handled with a nested-savepoint insert that
    swallows a duplicate-key race (the async probe and the reconciliation sweep
    can both target the same node) without rolling back the caller's outer
    transaction. Raises on non-duplicate DB failures — callers decide how to
    fail.
    """
    from modulo.db.models.run import Run
    from modulo.db.models.run_evidence import RunEvidence

    if organisation_id is None:
        organisation_id = await session.scalar(select(Run.organisation_id).where(Run.id == run_id))
        if organisation_id is None:
            _log.warning(
                "heuristic.evidence_write_skipped_no_tenant",
                extra={"run_id": str(run_id), "node_id": node_id, "evidence_state": evidence_state},
            )
            return

    # ``node_id`` arrives as a ``str`` from stored ``outputs_json``/
    # ``telemetry_json`` keys, which for legacy runs can hold non-UUID values.
    # Cast leniently (matching every other boundary in this PR) and skip rather
    # than raise inside the write path - a throwing ``UUID(node_id)`` would drop
    # the evidence row and only log a metric (the advertised no-crash behaviour).
    node_uuid = coerce_uuid(node_id)
    if node_uuid is None:
        _log.warning(
            "heuristic.evidence_write_skipped_unparseable_node_id",
            extra={"run_id": str(run_id), "node_id": node_id, "evidence_state": evidence_state},
        )
        return

    try:
        async with session.begin_nested():
            session.add(
                RunEvidence(
                    organisation_id=organisation_id,
                    run_id=run_id,
                    node_id=node_uuid,
                    evidence_state=evidence_state,
                    evidence_detail=evidence_detail,
                )
            )
    except IntegrityError:
        # A concurrent probe/sweep already wrote this (run_id, node_id) — the
        # row is already correct; the savepoint rollback kept the outer tx intact.
        return


# --- concrete provider ------------------------------------------------------


async def _e2b_run_command(sandbox_id: str, command: str) -> CommandResult:
    """Run a shell command in a live E2B sandbox. Single-shot SDK calls are
    individually bounded (repo rule: every E2B SDK call under asyncio.wait_for);
    connect/run are fresh coroutines safe to cancel. Raises on failure — the
    caller maps any raised error to unverifiable.
    """
    from e2b import AsyncSandbox

    sandbox = await asyncio.wait_for(AsyncSandbox.connect(sandbox_id), timeout=_SANDBOX_IO_TIMEOUT_SECONDS)
    try:
        result = await asyncio.wait_for(sandbox.commands.run(command), timeout=_SANDBOX_IO_TIMEOUT_SECONDS)
        return CommandResult(
            exit_code=int(getattr(result, "exit_code", 1) or 1),
            stdout=str(getattr(result, "stdout", "") or ""),
            stderr=str(getattr(result, "stderr", "") or ""),
        )
    finally:
        await sandbox.close()


async def _e2b_list_files(sandbox_id: str) -> list[FileInfo]:
    """List the live E2B sandbox's user home. Raises on failure — the caller
    maps any raised error to unverifiable.
    """
    from e2b import AsyncSandbox

    sandbox = await asyncio.wait_for(AsyncSandbox.connect(sandbox_id), timeout=_SANDBOX_IO_TIMEOUT_SECONDS)
    try:
        entries = await asyncio.wait_for(
            sandbox.files.list("/home/user"),
            timeout=_SANDBOX_IO_TIMEOUT_SECONDS,
        )
        files: list[FileInfo] = []
        for entry in entries:
            name = str(getattr(entry, "name", "") or "")
            if name:
                files.append(
                    FileInfo(
                        name=name,
                        size=int(getattr(entry, "size", 0) or 0),
                        is_dir=bool(getattr(entry, "isdir", False)),
                    )
                )
        return files
    finally:
        await sandbox.close()


def build_default_evidence_provider(
    session_factory: Callable[[], AsyncSession],
    org_id: UUID,
) -> SandboxEvidenceProvider:
    """Wire the production provider: DB-backed sandbox resolution + stored
    output_json loading, E2B-SDK command/files probing.
    """

    async def _resolve_sandbox_id(run_id: UUID, _node_id: str) -> str | None:
        from modulo.db.crud.run import get_run
        from modulo.db.rls import set_rls_org

        async with session_factory() as session, session.begin():
            await set_rls_org(session, org_id)
            run = await get_run(session, run_id)
            return run.sandbox_id if run is not None else None

    async def _load_output_json(run_id: UUID, node_id: str) -> dict[str, Any] | None:
        from modulo.db.crud.run import get_run
        from modulo.db.rls import set_rls_org

        async with session_factory() as session, session.begin():
            await set_rls_org(session, org_id)
            run = await get_run(session, run_id)
            if run is None:
                return None
            return extract_stored_output_json(run.outputs_json, run.node_telemetry_json, node_id)

    return SandboxEvidenceProvider(
        sandbox_id_resolver=_resolve_sandbox_id,
        output_json_loader=_load_output_json,
        run_command=_e2b_run_command,
        list_files=_e2b_list_files,
    )


class SandboxEvidenceProvider:
    """Concrete EvidenceProvider probing the run's E2B sandbox.

    Each method applies the bounded ``asyncio.wait_for`` internally (≤3s) and
    returns ``unverifiable`` for deterministic unavailability (no sandbox, no
    repo, no runner wired). Timeouts and raised SDK errors PROPAGATE so the
    caller can distinguish a probe error (``modulo_heuristic_errors_total``)
    from an unverifiable outcome (§15.14).
    """

    def __init__(
        self,
        *,
        sandbox_id_resolver: Callable[[UUID, str], Awaitable[str | None]] | None = None,
        run_command: Callable[[str, str], Awaitable[CommandResult]] | None = None,
        list_files: Callable[[str], Awaitable[list[FileInfo]]] | None = None,
        output_json_loader: Callable[[UUID, str], Awaitable[dict[str, Any] | None]] | None = None,
        timeout_seconds: float = EVIDENCE_PROBE_TIMEOUT_SECONDS,
    ) -> None:
        self._sandbox_id_resolver = sandbox_id_resolver
        self._run_command = run_command
        self._list_files = list_files
        self._output_json_loader = output_json_loader
        self._timeout_seconds = timeout_seconds

    async def _resolve_sandbox_id(self, run_id: UUID, node_id: str) -> str | None:
        if self._sandbox_id_resolver is None:
            return None
        value = self._sandbox_id_resolver(run_id, node_id)
        if inspect.isawaitable(value):
            return await value
        return value

    async def _load_output_json(self, run_id: UUID, node_id: str) -> dict[str, Any] | None:
        if self._output_json_loader is None:
            return None
        value = self._output_json_loader(run_id, node_id)
        if inspect.isawaitable(value):
            return await value
        return value

    async def git_diff_empty(self, run_id: UUID, node_id: str) -> EvidenceResult:
        """§15.3 git probe: the run's git diff is empty (whitespace ignored)
        AND no non-metadata output_json key. Timeout/exception/no-repo →
        unverifiable.
        """
        return await asyncio.wait_for(
            self._git_diff_empty_unbounded(run_id, node_id),
            timeout=self._timeout_seconds,
        )

    async def _git_diff_empty_unbounded(self, run_id: UUID, node_id: str) -> EvidenceResult:
        sandbox_id = await self._resolve_sandbox_id(run_id, node_id)
        if not sandbox_id:
            return EvidenceResult.unverifiable
        if self._run_command is None:
            return EvidenceResult.unverifiable
        is_repo = await self._run_command(sandbox_id, "git rev-parse --is-inside-work-tree")
        if is_repo.exit_code != 0:
            return EvidenceResult.unverifiable
        diff = await self._run_command(sandbox_id, "git diff -w --quiet")
        status = await self._run_command(sandbox_id, "git status --porcelain --untracked-files=normal")
        untracked = any(line.startswith("?? ") for line in status.stdout.splitlines())
        if diff.exit_code != 0 or untracked:
            return EvidenceResult.has_work
        output_json = await self._load_output_json(run_id, node_id)
        if self._output_json_loader is None:
            return EvidenceResult.unverifiable
        if output_json_has_content(output_json):
            return EvidenceResult.has_work
        return EvidenceResult.verified_empty

    async def sandbox_filesystem_probe(self, run_id: UUID, node_id: str) -> EvidenceResult:
        """§15.3 sandbox-filesystem probe: fs with content → has_work; fs
        without content → verified_empty; no sandbox → unverifiable.
        """
        return await asyncio.wait_for(
            self._sandbox_filesystem_probe_unbounded(run_id, node_id),
            timeout=self._timeout_seconds,
        )

    async def _sandbox_filesystem_probe_unbounded(self, run_id: UUID, node_id: str) -> EvidenceResult:
        sandbox_id = await self._resolve_sandbox_id(run_id, node_id)
        if not sandbox_id:
            return EvidenceResult.unverifiable
        if self._list_files is None:
            return EvidenceResult.unverifiable
        files = await self._list_files(sandbox_id)
        if any(not f.is_dir and f.size > 0 for f in files):
            return EvidenceResult.has_work
        return EvidenceResult.verified_empty


def combine_probe_results(*results: EvidenceResult) -> tuple[EvidenceResult, str]:
    """§15.3 combination rule: any positive → has_work (never false-flag); any
    unverifiable → unverifiable (never flag); only when EVERY probe confirms
    empty → verified_empty (the no-op flag's input).
    """
    if any(r == EvidenceResult.has_work for r in results):
        return EvidenceResult.has_work, "positive evidence (git diff / sandbox filesystem)"
    if any(r == EvidenceResult.unverifiable for r in results):
        return EvidenceResult.unverifiable, "evidence could not be verified"
    return EvidenceResult.verified_empty, (
        "git diff empty (whitespace ignored), no non-metadata output_json key, sandbox filesystem empty"
    )


def _estimate_probe_cost_usd(seconds: float) -> float:
    """Estimated marginal sandbox cost of a probe: E2B hourly rate x probe
    wall-clock. There is no LLM/token cost — the probe executes shell commands
    and file listings only. Rate read from settings at runtime (mirrors
    node_runner), defaulting to the constant.
    """
    rate = _PROBE_SANDBOX_USD_PER_HOUR
    try:
        from modulo.settings import get_settings

        rate = float(getattr(get_settings(), "e2b_sandbox_usd_per_hour", _PROBE_SANDBOX_USD_PER_HOUR))
    except Exception:
        _log.debug("heuristic.probe_cost.rate_lookup_failed; using default", exc_info=True)
    return round(rate * (seconds / 3600.0), 12)


async def run_evidence_probe(
    *,
    provider: EvidenceProvider,
    session_factory: Callable[[], AsyncSession],
    run_id: UUID,
    node_id: str,
    organisation_id: UUID,
) -> EvidenceResult:
    """Run the bounded (≤3s) async evidence probe for one node and persist the
    run_evidence row. Runs POST-commit, off the run's critical path (§15.3).

    ``organisation_id`` is the parent run's org — the run_evidence table is
    org-scoped (0133), so the write sets RLS to that org and threads it onto
    the row.

    Fail-open: any probe/write error records the §15.14 metrics and degrades to
    an 'unverifiable' row — the run's terminalization is never affected. The
    caller (executor or reconciliation sweep) is responsible for gating on
    declared-success nodes.
    """
    if not evidence_enabled():
        return EvidenceResult.unverifiable
    from modulo.db.rls import set_rls_org

    started = time.monotonic()
    state: EvidenceResult = EvidenceResult.unverifiable
    detail: str = "evidence could not be verified"
    try:
        git_result = await asyncio.wait_for(
            provider.git_diff_empty(run_id, node_id),
            timeout=EVIDENCE_PROBE_TIMEOUT_SECONDS,
        )
        fs_result = await asyncio.wait_for(
            provider.sandbox_filesystem_probe(run_id, node_id),
            timeout=EVIDENCE_PROBE_TIMEOUT_SECONDS,
        )
        state, detail = combine_probe_results(git_result, fs_result)
    except TimeoutError:
        detail = "probe exceeded the bounded window"
        _log.warning("heuristic.unverifiable", extra={"run_id": str(run_id), "node_id": node_id, "detail": detail})
    except asyncio.CancelledError:
        raise
    except Exception:
        record_heuristic_error("probe_raised")
        _log.exception("heuristic.probe_error", extra={"run_id": str(run_id), "node_id": node_id})
        detail = "probe raised an error"
    finally:
        elapsed = time.monotonic() - started
        record_heuristic_probe_latency(elapsed)
        record_heuristic_probe_cost(_estimate_probe_cost_usd(elapsed))
    if state == EvidenceResult.unverifiable:
        record_heuristic_unverifiable(detail)
    try:
        async with session_factory() as session, session.begin():
            await set_rls_org(session, organisation_id)
            await write_evidence_row(
                session,
                run_id=run_id,
                node_id=node_id,
                evidence_state=str(state.value),
                evidence_detail=detail,
                organisation_id=organisation_id,
            )
    except asyncio.CancelledError:
        raise
    except Exception:
        record_heuristic_error("evidence_write_failed")
        _log.exception("heuristic.evidence_write_failed", extra={"run_id": str(run_id), "node_id": node_id})
    return state


# --- bounded reconciliation sweep ------------------------------------------


async def reconcile_noop_evidence(
    session_factory: Callable[[], AsyncSession],
    *,
    provider: EvidenceProvider,
    max_runs: int = 20,
    budget_seconds: float = 30.0,
) -> dict[str, int]:
    """Bounded one-shot reconciliation sweep for no-op-eligible runs that
    missed the async probe window (§15.3/§13.3 — closes the crash-window
    detection hole). Finds recent terminal 'complete' runs with
    declared-success nodes that have no run_evidence row and re-runs the
    probe for them.

    Bounded against runaway: processes at most *max_runs* runs per invocation
    and returns early once *budget_seconds* elapses. Wiring to a periodic path
    (SAQ cron / housekeeping) is deferred — this is the opportunistic entry
    point a caller invokes.

    Returns ``{"scanned", "probed", "has_work", "verified_empty",
    "unverifiable", "errors"}``. Never raises on a single probe failure.
    """
    from modulo.db.models.run import Run
    from modulo.db.models.run_evidence import RunEvidence

    summary: dict[str, int] = {
        "scanned": 0,
        "probed": 0,
        "has_work": 0,
        "verified_empty": 0,
        "unverifiable": 0,
        "errors": 0,
    }
    if not evidence_enabled():
        return summary
    deadline = time.monotonic() + budget_seconds

    async with session_factory() as session, session.begin():
        result = await session.execute(
            select(Run)
            .where(Run.status.in_(RECONCILE_COMPLETE_STATUSES))
            .order_by(Run.completed_at.desc())
            .limit(max_runs)
        )
        runs = list(result.scalars().all())
        existing: set[tuple[UUID, str]] = set()
        if runs:
            evidence_rows = await session.execute(
                select(RunEvidence.run_id, RunEvidence.node_id).where(RunEvidence.run_id.in_([run.id for run in runs]))
            )
            existing = {(row.run_id, str(row.node_id)) for row in evidence_rows.all()}

    for run in runs:
        summary["scanned"] += 1
        if time.monotonic() > deadline:
            break
        for node_id in _declared_success_nodes(run.outputs_json, run.node_telemetry_json):
            if (run.id, node_id) in existing:
                continue
            summary["probed"] += 1
            try:
                state = await run_evidence_probe(
                    provider=provider,
                    session_factory=session_factory,
                    run_id=run.id,
                    node_id=node_id,
                    organisation_id=run.organisation_id,
                )
                summary[str(state.value)] += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                summary["errors"] += 1
                _log.exception(
                    "heuristic.sweep_probe_failed",
                    extra={"run_id": str(run.id), "node_id": node_id},
                )
            if time.monotonic() > deadline:
                break
    return summary
