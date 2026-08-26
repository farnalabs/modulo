"""CRUD for VariantGroup — A/B test variant management.

All functions require RLS org context to be set by the caller.
"""

import logging
import secrets
import uuid
from datetime import UTC, datetime
from typing import Any, NamedTuple

from sqlalchemy import case, func, select
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.agent import get_agent
from modulo.db.crud.eval_run import non_guardrail_eval_results_clause
from modulo.db.crud.run import count_active_runs_for_pipeline, create_run
from modulo.db.models.eval_definition import EvalDefinition
from modulo.db.models.eval_result import EvalResult
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.pipeline_snapshot import PipelineSnapshot
from modulo.db.models.run import Run
from modulo.db.models.variant_group import VariantGroup

_log = logging.getLogger(__name__)


class _RunDispatch(NamedTuple):
    """Dispatch parameters shared by every run created from a variant group."""

    org_id: uuid.UUID
    account_id: uuid.UUID | None
    trigger_type: str


async def create_variant_group(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    name: str,
    variants: list[dict[str, Any]],
    description: str | None = None,
    selection_strategy: str = "weighted",
    max_concurrent_runs: int = 5,
    degraded_evals: bool = False,
) -> VariantGroup:
    group = VariantGroup(
        organisation_id=org_id,
        pipeline_id=pipeline_id,
        name=name,
        description=description,
        variants=variants,
        selection_strategy=selection_strategy,
        max_concurrent_runs=max_concurrent_runs,
        degraded_evals=degraded_evals,
    )
    session.add(group)
    await session.flush()
    return group


async def get_variant_group(
    session: AsyncSession, group_id: uuid.UUID, *, include_deleted: bool = False
) -> VariantGroup | None:
    stmt = select(VariantGroup).where(VariantGroup.id == group_id)
    if not include_deleted:
        stmt = stmt.where(VariantGroup.deleted_at.is_(None))
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def list_variant_groups(
    session: AsyncSession,
    *,
    pipeline_id: uuid.UUID | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[VariantGroup], int]:
    q = (
        select(VariantGroup)
        .join(Pipeline, Pipeline.id == VariantGroup.pipeline_id)
        .where(Pipeline.deleted_at.is_(None), VariantGroup.deleted_at.is_(None))
    )
    count_q = (
        select(func.count())
        .select_from(VariantGroup)
        .join(Pipeline, Pipeline.id == VariantGroup.pipeline_id)
        .where(Pipeline.deleted_at.is_(None), VariantGroup.deleted_at.is_(None))
    )
    if pipeline_id is not None:
        q = q.where(VariantGroup.pipeline_id == pipeline_id)
        count_q = count_q.where(VariantGroup.pipeline_id == pipeline_id)

    offset = (page - 1) * page_size
    try:
        total = (await session.execute(count_q)).scalar_one()
    except ProgrammingError:
        _log.warning("variant_group table not found — returning empty list", exc_info=True)
        return [], 0
    items = list(
        (await session.execute(q.order_by(VariantGroup.created_at.desc()).offset(offset).limit(page_size))).scalars()
    )
    return items, total


async def update_variant_group(
    session: AsyncSession,
    group_id: uuid.UUID,
    *,
    name: str | None = None,
    description: str | None = None,
    variants: list[dict[str, Any]] | None = None,
    selection_strategy: str | None = None,
    max_concurrent_runs: int | None = None,
    degraded_evals: bool | None = None,
) -> VariantGroup | None:
    group = await get_variant_group(session, group_id)
    if group is None:
        return None
    if name is not None:
        group.name = name
    if description is not None:
        group.description = description
    if variants is not None:
        group.variants = variants
    if selection_strategy is not None:
        group.selection_strategy = selection_strategy
    if max_concurrent_runs is not None:
        group.max_concurrent_runs = max_concurrent_runs
    if degraded_evals is not None:
        group.degraded_evals = degraded_evals
    await session.flush()
    return group


async def soft_delete_variant_group(session: AsyncSession, group_id: uuid.UUID) -> bool:
    group = await get_variant_group(session, group_id)
    if group is None:
        return False
    group.deleted_at = datetime.now(UTC)
    await session.flush()
    return True


async def restore_variant_group(session: AsyncSession, group_id: uuid.UUID) -> VariantGroup | None:
    group = await get_variant_group(session, group_id, include_deleted=True)
    if group is None or group.deleted_at is None:
        return None
    group.deleted_at = None
    await session.flush()
    return group


async def increment_run_count(session: AsyncSession, group_id: uuid.UUID, *, delta: int = 1) -> VariantGroup | None:
    result = await session.execute(select(VariantGroup).where(VariantGroup.id == group_id).with_for_update())
    group = result.scalar_one_or_none()
    if group is None:
        return None
    group.run_count = (group.run_count or 0) + delta
    await session.flush()
    return group


async def check_pipeline_run_quota(session: AsyncSession, group: VariantGroup) -> bool:
    """Check if the pipeline is within its concurrent run quota.

    Returns True if a new run is allowed, False if quota is exceeded.
    """
    active = await count_active_runs_for_pipeline(session, group.pipeline_id, include_pending=True)
    return active < group.max_concurrent_runs


async def check_pipeline_run_quota_for_batch(session: AsyncSession, group: VariantGroup, batch_size: int) -> bool:
    """Check whether firing ``batch_size`` runs at once stays within quota.

    All-or-nothing pre-flight: requires headroom for the entire batch, not just
    one run, so the group is never partially fired.
    """
    active = await count_active_runs_for_pipeline(session, group.pipeline_id, include_pending=True)
    return active + batch_size <= group.max_concurrent_runs


def pick_variant_weighted(
    variants: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Select a variant using weighted random selection.

    Each variant dict should contain a 'weight' key (default 1.0).
    If only one variant, returns it directly (short-circuit).
    """
    if not variants:
        return None
    clean = [v for v in variants if isinstance(v, dict)]
    if not clean:
        return None
    if len(clean) == 1:
        return clean[0]

    weights = [float(v.get("weight", 1.0)) for v in clean]
    total = sum(weights)
    if total <= 0:
        return secrets.choice(clean)  # NOSONAR S2245 — A/B traffic split, not a security boundary

    r = (secrets.randbelow(1_000_000) / 1_000_000) * total  # NOSONAR S2245 — A/B traffic split, not a security boundary
    cumulative = 0.0
    for i, w in enumerate(weights):
        cumulative += w
        if r <= cumulative:
            return clean[i]
    return clean[-1]


async def _lock_variant_group(session: AsyncSession, group: VariantGroup) -> VariantGroup | None:
    """Row-lock the group to prevent concurrent quota races (PRD 8.19)."""
    result = await session.execute(select(VariantGroup).where(VariantGroup.id == group.id).with_for_update())
    locked = result.scalar_one_or_none()
    return locked if locked is not None else None


def _coerce_snapshot_id(raw: Any) -> uuid.UUID | None:
    """Normalise a variant's ``snapshot_id`` to a UUID (or ``None``)."""
    if raw is None:
        return None
    return uuid.UUID(str(raw)) if isinstance(raw, str) else raw


# Control override keys are stored in the run's frozen ``variant_config_snapshot``
# under the system-reserved ``_run_overrides`` namespace (seeded into run_context by
# the executor — NEVER from caller input, see ``_merge_variant_payload`` / executor).
#
# FAR-343 verification conclusion: these two keys target DIFFERENT node types.
#   - ``model_backend_id`` (a Modulo model-backend UUID) is consumed by the
#     node_runner's ``agent`` node (single-shot LLM) path only — the node resolves
#     the backend by UUID from ``_run_overrides["model_backend_id"]``.
#   - ``model`` (an opencode model ID) is for ``sandbox_agent`` nodes. The
#     ``agent_command`` is Jinja-rendered with ``run_context`` in scope, so a
#     pipeline author can vary the opencode model per-run by writing
#     ``--model {{ run_context._run_overrides.model }}`` in the command. It does
#     NOT map to a Modulo model-backend UUID — it is the opencode CLI model ID
#     (e.g. ``opencode-go/hy3``) baked into the E2B sandbox command.
_CONTROL_OVERRIDE_KEYS = ("model_backend_id", "prompt_version", "model")


def _extract_agent_ids_from_graph(graph_json: Any) -> set[uuid.UUID]:
    """Collect the ``agent_id`` set from a snapshot's pipeline graph JSON."""
    agent_ids: set[uuid.UUID] = set()
    for node in graph_json.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        agent_id = node.get("agent_id")
        if not agent_id:
            continue
        try:
            agent_ids.add(uuid.UUID(str(agent_id)))
        except (ValueError, TypeError):
            continue
    return agent_ids


def _agent_template_for_version(agent: Any, prompt_version: str) -> str | None:
    """Resolve a single agent's active or versioned prompt template."""
    if prompt_version == "current":
        template = agent.prompt_template
        return template if isinstance(template, str) and template else None
    for entry in agent.prompt_version_history or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("version") == prompt_version:
            tpl = entry.get("template")
            if isinstance(tpl, str) and tpl:
                return tpl
    return None


async def _resolve_prompt_template_override(
    session: AsyncSession,
    *,
    snapshot_id: uuid.UUID,
    prompt_version: str,
) -> dict[str, str]:
    """Resolve a variant's ``prompt_version`` label to a PER-AGENT template map.

    Loads the snapshot's agent nodes to gather the ``agent_id`` set, iterates
    them in DETERMINISTIC (sorted) order, and queries each agent's
    ``prompt_version_history`` for the entry whose ``version`` matches the
    override. Returns ``{agent_id: resolved_template}`` containing ONLY the
    agents that carry the requested version (or, for ``"current"``, their
    active ``prompt_template``). An EMPTY dict when no agent carries the
    version — the node then falls back to its snapshot-embedded prompt.

    Keying by agent (instead of a single run-wide template) means one agent's
    template can never clobber another's in a multi-agent snapshot. This
    mirrors the version resolution in
    ``modulo.api.routes.agents._resolve_prompt_template``.
    """
    snap_result = await session.execute(select(PipelineSnapshot.id).where(PipelineSnapshot.id == snapshot_id))
    if snap_result.scalar_one_or_none() is None:
        return {}

    graph_result = await session.execute(select(PipelineSnapshot.graph_json).where(PipelineSnapshot.id == snapshot_id))
    graph_json = graph_result.scalar_one_or_none()
    if not isinstance(graph_json, dict):
        return {}

    agent_ids = _extract_agent_ids_from_graph(graph_json)

    resolved: dict[str, str] = {}
    for agent_id in sorted(agent_ids):
        agent = await get_agent(session, agent_id)
        if agent is None:
            continue
        template = _agent_template_for_version(agent, prompt_version)
        if template:
            resolved[str(agent_id)] = template
    return resolved


def _merge_variant_payload(
    variant: dict[str, Any],
    base_payload: dict[str, Any],
    *,
    degraded_evals: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Merge a variant's ``run_context_overrides`` into the base payload.

    Returns ``(payload, control_overrides)``. Neither *base_payload* nor
    *variant* is mutated. Any caller-supplied ``_run_overrides`` in
    *base_payload* is STRIPPED (the namespace is system-reserved — a crafted
    dict is a prompt-injection vector). Control keys (``model_backend_id``,
    ``prompt_version``) are returned SEPARATELY in ``control_overrides`` — they
    are stored in the run's ``variant_config_snapshot`` (seeded into run_context
    by the executor), NEVER written back into the payload, so a legitimate data
    field named ``model_backend_id`` in user-supplied input can never silently
    reroute model routing. Any other (data) override still merges into the
    payload at the top level as before. When ``degraded_evals`` is set the
    ``_degraded_evals`` marker is applied last so the group setting always wins
    over any override.
    """
    payload = dict(base_payload)
    # The ``_run_overrides`` namespace is system-reserved. ANY caller-supplied
    # value in the base payload must be STRIPPED — a crafted ``_run_overrides``
    # dict (e.g. ``{"prompt_templates": {<agent_id>: "injected prompt"}}``) is
    # an arbitrary prompt-injection vector that would otherwise survive the
    # merge and override the rendered prompt. The system re-populates ONLY the
    # control keys it sets from ``run_context_overrides`` below (and later the
    # resolved ``prompt_templates``), never trusting caller input.
    payload.pop("_run_overrides", None)
    overrides = variant.get("run_context_overrides", {})
    controls: dict[str, Any] = {}
    if isinstance(overrides, dict):
        controls = {k: overrides[k] for k in _CONTROL_OVERRIDE_KEYS if k in overrides}
        data_overrides = {k: v for k, v in overrides.items() if k not in _CONTROL_OVERRIDE_KEYS}
        if data_overrides:
            payload.update(data_overrides)
    if degraded_evals:
        payload["_degraded_evals"] = True
    return payload, controls


async def run_variant_weighted(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    group: VariantGroup,
    input_payload: dict[str, Any] | None = None,
    account_id: uuid.UUID | None = None,
    trigger_type: str = "manual",
) -> dict[str, Any] | None:
    """Select a variant, merge its run_context_overrides, and create a run.

    Returns dict with run_id, variant, merged_payload, or None if quota exceeded.
    Locks the variant group row to prevent concurrent quota races.
    """
    locked = await _lock_variant_group(session, group)
    if locked is None:
        return None
    group = locked

    if not await check_pipeline_run_quota(session, group):
        return None

    variant = pick_variant_weighted(group.variants)
    if variant is None:
        return None

    snapshot_id = _coerce_snapshot_id(variant.get("snapshot_id"))
    if snapshot_id is None:
        return None

    merged_payload, control_overrides = _merge_variant_payload(
        variant, input_payload or {}, degraded_evals=group.degraded_evals
    )

    # FAR-342: resolve a prompt_version override to its per-agent templates (see
    # the batch path for the full comment). The override namespace lives in the
    # frozen ``variant_config_snapshot`` — the executor seeds it into run_context
    # as a top-level system-reserved key, NEVER from caller input.
    if control_overrides.get("prompt_version"):
        resolved = await _resolve_prompt_template_override(
            session,
            snapshot_id=snapshot_id,
            prompt_version=str(control_overrides["prompt_version"]),
        )
        if resolved:
            control_overrides["prompt_templates"] = resolved

    # FAR-381: a weighted single-shot run MUST carry variant identity in its
    # frozen snapshot, or the coverage-gap read-model (which keys divergence by
    # ``variant_id`` / ``variant_name``) silently skips it — a weighted group
    # then reports ``no gap`` despite real divergence. Parity with the batch
    # path: ``variant_id`` (stable persisted id, may be absent on legacy
    # variants), ``variant_name``, ``snapshot_id``, ``run_context_overrides``,
    # and the system-reserved ``_run_overrides``. ``batch_id`` is intentionally
    # omitted — these are single-shot runs, not a fired batch.
    overrides = variant.get("run_context_overrides", {})
    if not isinstance(overrides, dict):
        overrides = {}
    frozen_snapshot: dict[str, Any] = {
        "variant_id": str(variant["id"]) if variant.get("id") is not None else None,
        "variant_name": variant.get("name"),
        "snapshot_id": str(snapshot_id),
        "run_context_overrides": dict(overrides),
        "_run_overrides": control_overrides,
    }

    run = await create_run(
        session,
        org_id=org_id,
        pipeline_id=group.pipeline_id,
        snapshot_id=snapshot_id,
        trigger_type=trigger_type,
        input_payload=merged_payload,
        account_id=account_id,
        variant_group_id=group.id,
        variant_config_snapshot=frozen_snapshot,
    )

    await increment_run_count(session, group.id)

    return {
        "run_id": run.id,
        "variant": variant,
        "merged_payload": merged_payload,
        "frozen_snapshot": frozen_snapshot,
    }


async def _fire_batch_variant(
    session: AsyncSession,
    *,
    group: VariantGroup,
    variant: dict[str, Any],
    input_payload: dict[str, Any] | None,
    dispatch: _RunDispatch,
    batch_id: uuid.UUID,
) -> dict[str, Any] | None:
    """Create one run for a single variant in a batch (PRD 8.19).

    Merges the variant's ``run_context_overrides``, resolves any
    ``prompt_version`` override to its per-agent templates, and freezes the
    variant snapshot at fire time. Returns the run summary dict, or ``None``
    when the variant is missing a ``snapshot_id`` (defensive — the batch
    pre-flight already guarantees every variant has one).
    """
    overrides = variant.get("run_context_overrides", {})
    if not isinstance(overrides, dict):
        overrides = {}
    snapshot_id = _coerce_snapshot_id(variant.get("snapshot_id"))
    if snapshot_id is None:
        return None

    payload, control_overrides = _merge_variant_payload(
        variant, input_payload or {}, degraded_evals=group.degraded_evals
    )

    # FAR-342: resolve a prompt_version override to its per-agent templates
    # so the node_runner can render the selected prompt for each node, not
    # the snapshot-embedded one — keyed by agent so one agent's template
    # never clobbers another's. Stored alongside the version label under
    # ``_run_overrides`` in the frozen snapshot (the executor seeds it into
    # run_context as a top-level system-reserved key, NEVER from caller
    # input).
    if control_overrides.get("prompt_version"):
        resolved = await _resolve_prompt_template_override(
            session,
            snapshot_id=snapshot_id,
            prompt_version=str(control_overrides["prompt_version"]),
        )
        if resolved:
            control_overrides["prompt_templates"] = resolved

    # Frozen snapshot/override capture at fire time — the single source of
    # truth for "which input this variant ran with". The compare view reads
    # this, never the live snapshot, so later group edits cannot rewrite
    # history. ``variant_id`` is the stable persisted id (frontend-minted
    # on Duplicate, FAR-332 3b); it may be absent on legacy variants. The
    # system-reserved ``_run_overrides`` (model_backend_id / prompt_version /
    # resolved prompt_templates) rides here, never in the input payload.
    frozen_snapshot = {
        "variant_id": str(variant["id"]) if variant.get("id") is not None else None,
        "variant_name": variant.get("name"),
        "snapshot_id": str(snapshot_id),
        "run_context_overrides": dict(overrides),
        "_run_overrides": control_overrides,
        "batch_id": str(batch_id),
    }

    run = await create_run(
        session,
        org_id=dispatch.org_id,
        pipeline_id=group.pipeline_id,
        snapshot_id=snapshot_id,
        trigger_type=dispatch.trigger_type,
        input_payload=payload,
        account_id=dispatch.account_id,
        variant_group_id=group.id,
        batch_id=batch_id,
        variant_config_snapshot=frozen_snapshot,
    )
    return {
        "run_id": run.id,
        "batch_id": batch_id,
        "variant": variant,
        "merged_payload": payload,
        "frozen_snapshot": frozen_snapshot,
    }


async def run_variant_batch(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    group: VariantGroup,
    input_payload: dict[str, Any] | None = None,
    account_id: uuid.UUID | None = None,
    trigger_type: str = "manual",
) -> list[dict[str, Any]] | None:
    """Fire one run per variant, all-or-nothing (PRD 8.19).

    A batch fires ``len(group.variants)`` runs (one per variant, in variant
    insertion order), each sharing the same ``input_payload`` with the variant's
    ``run_context_overrides`` merged on top. Before any run is created the group
    is pre-flighted: at least one variant must exist, every variant must carry a
    ``snapshot_id``, and the pipeline concurrent-run quota must fit the whole
    batch at once (``active + N <= max_concurrent_runs``). If any pre-flight
    check fails the entire group is rejected (returns ``None``) — no partial
    firing ever happens.

    Returns a list of ``{run_id, variant, merged_payload}`` in variant insertion
    order, or ``None`` when the group cannot fire.
    """
    locked = await _lock_variant_group(session, group)
    if locked is None:
        return None
    group = locked

    variants = [v for v in group.variants if isinstance(v, dict)]
    if not variants:
        return None

    for variant in variants:
        if variant.get("snapshot_id") is None:
            return None

    batch_size = len(variants)
    if not await check_pipeline_run_quota_for_batch(session, group, batch_size):
        return None

    # FAR-332 3c — one batch = one batch_id. Every run created below is stamped
    # with the same value so the compare route can load the batch purely by it.
    batch_id = uuid.uuid4()

    dispatch = _RunDispatch(org_id=org_id, account_id=account_id, trigger_type=trigger_type)

    results: list[dict[str, Any]] = []
    for variant in variants:
        entry = await _fire_batch_variant(
            session,
            group=group,
            variant=variant,
            input_payload=input_payload,
            dispatch=dispatch,
            batch_id=batch_id,
        )
        if entry is None:
            return None  # defensive — the pre-flight loop above already guarantees this
        results.append(entry)

    await increment_run_count(session, group.id, delta=batch_size)
    return results


async def get_coverage_gaps(
    session: AsyncSession,
    group: VariantGroup,
    *,
    eval_def_ids: list[uuid.UUID] | None = None,
) -> list[dict[str, Any]]:
    """Detect which variants lack eval definitions.

    Returns a list of gaps: [{variant: …, missing_evals: [str, …]}, …].
    """
    if eval_def_ids is None:
        result = await session.execute(select(EvalDefinition).where(EvalDefinition.pipeline_id == group.pipeline_id))
        eval_defs = list(result.scalars())
        eval_def_ids = [e.id for e in eval_defs]

    gaps: list[dict[str, Any]] = []
    for variant in group.variants:
        variant_eval_ids = {uuid.UUID(str(eid)) for eid in variant.get("eval_definition_ids", [])}
        missing = [str(eid) for eid in eval_def_ids if eid not in variant_eval_ids]
        if missing:
            gaps.append(
                {
                    "variant": variant,
                    "missing_evals": missing,
                }
            )
    return gaps


def _prompt_pins(snapshot: Any) -> dict[str, str | None]:
    """Map agent_id → prompt_version_hash from a snapshot's prompt_pins_json."""
    raw = snapshot.prompt_pins_json
    if not isinstance(raw, list):
        return {}
    return {p.get("agent_id"): p.get("prompt_version_hash") for p in raw if p.get("agent_id")}


def _prompt_diff_for_pair(
    base_variant: dict[str, Any],
    variant: dict[str, Any],
    snapshots: dict[uuid.UUID, Any],
) -> dict[str, Any] | None:
    """Compute the agent-level prompt diff between a base and comparison variant."""
    base_id = _coerce_snapshot_id(base_variant.get("snapshot_id"))
    variant_id = _coerce_snapshot_id(variant.get("snapshot_id"))
    base_snapshot = snapshots.get(base_id) if base_id else None
    variant_snapshot = snapshots.get(variant_id) if variant_id else None
    if base_snapshot is None or variant_snapshot is None:
        return None

    base_pins = _prompt_pins(base_snapshot)
    variant_pins = _prompt_pins(variant_snapshot)

    agent_diffs: list[dict[str, str | None]] = []
    for agent_id, variant_hash in variant_pins.items():
        base_hash = base_pins.get(agent_id)
        if base_hash and base_hash != variant_hash:
            agent_diffs.append(
                {
                    "agent_id": agent_id,
                    "base_hash": base_hash,
                    "variant_hash": variant_hash,
                }
            )
    if not agent_diffs:
        return None
    return {
        "base_variant": base_variant,
        "variant": variant,
        "agent_diffs": agent_diffs,
    }


async def get_prompt_diffs(
    session: AsyncSession,
    group: VariantGroup,
    *,
    base_snapshot_ids: list[uuid.UUID] | None = None,
) -> list[dict[str, Any]]:
    """Compare prompt_pins_json across variant snapshots.

    Returns a list of diff entries showing which agents have different
    prompt version hashes between the base snapshots and each variant.
    """
    snapshot_ids: set[uuid.UUID] = set()
    for variant in group.variants:
        sid = variant.get("snapshot_id")
        if sid is not None:
            snapshot_ids.add(uuid.UUID(str(sid)) if isinstance(sid, str) else sid)

    if base_snapshot_ids:
        snapshot_ids.update(base_snapshot_ids)

    if not snapshot_ids:
        return []

    result = await session.execute(select(PipelineSnapshot).where(PipelineSnapshot.id.in_(snapshot_ids)))
    snapshots = {s.id: s for s in result.scalars()}

    base_sids = set(base_snapshot_ids or [])
    base_variants = [v for v in group.variants if _coerce_snapshot_id(v.get("snapshot_id")) in base_sids]
    comparison_variants = [v for v in group.variants if _coerce_snapshot_id(v.get("snapshot_id")) not in base_sids]

    diffs: list[dict[str, Any]] = []
    for cv in comparison_variants:
        for bv in base_variants:
            entry = _prompt_diff_for_pair(bv, cv, snapshots)
            if entry is not None:
                diffs.append(entry)
    return diffs


async def validate_batch_ownership(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    snapshot_ids: list[uuid.UUID],
) -> bool:
    """Server-side ownership validation for the batch-run path (FAR-332 3f).

    Verifies the pipeline and every variant snapshot belong to the org. RLS
    already scopes reads; this is the explicit defence-in-depth check that
    turns a cross-org reference into a clear 403 (fail closed) instead of a
    generic FK error at insert time.
    """
    pipeline = await session.execute(
        select(Pipeline.id).where(Pipeline.id == pipeline_id, Pipeline.organisation_id == org_id)
    )
    if pipeline.scalar_one_or_none() is None:
        return False
    if not snapshot_ids:
        return True
    snap_result = await session.execute(
        select(PipelineSnapshot.id).where(
            PipelineSnapshot.id.in_(snapshot_ids),
            PipelineSnapshot.organisation_id == org_id,
        )
    )
    owned = set(snap_result.scalars())
    return all(sid in owned for sid in snapshot_ids)


async def has_pipeline_default_evals(session: AsyncSession, pipeline_id: uuid.UUID) -> bool:
    """Whether the pipeline has any default eval definitions (FAR-332 3g).

    A warn-not-block signal for the batch-run path: when the pipeline has no
    default evals the frontend shows "no evals → cost/diff only". Returned as a
    signal only — never a hard block.
    """
    result = await session.execute(select(EvalDefinition.id).where(EvalDefinition.pipeline_id == pipeline_id).limit(1))
    return result.scalar_one_or_none() is not None


async def get_batch_runs(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    batch_id: uuid.UUID,
) -> list[Run]:
    """Load a batch's runs purely by ``batch_id``, org-scoped (FAR-332 3d).

    Never joins a live variant group — the group may be soft-deleted and the
    batch must still be comparable. The explicit ``organisation_id`` predicate
    is the cross-org IDOR backstop on top of RLS.
    """
    result = await session.execute(
        select(Run).where(Run.organisation_id == org_id, Run.batch_id == batch_id).order_by(Run.created_at)
    )
    return list(result.scalars().all())


def _as_dict(raw: Any) -> dict[str, Any]:
    """Coerce a value to a dict (``{}`` for anything that is not a dict)."""
    return raw if isinstance(raw, dict) else {}


def _override_diff(base: dict[str, Any], current: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Split override differences into added / removed / changed sets."""
    added = {k: v for k, v in current.items() if k not in base}
    removed = {k: v for k, v in base.items() if k not in current}
    changed = {k: v for k, v in current.items() if k in base and base[k] != v}
    return {"added": added, "removed": removed, "changed": changed}


async def get_batch_compare(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    batch_id: uuid.UUID,
) -> list[dict[str, Any]]:
    """Per-run compare data for a batch (FAR-332 3d).

    Returns one entry per run in the batch (org-scoped, ordered by created_at),
    each carrying the canonical RUN_STATUS, eval pass rate, cost, tokens, and
    the frozen snapshot/override diff against the batch's base (first) run. The
    override diff is derived from the FROZEN ``variant_config_snapshot`` captured
    at fire time — never the live snapshot or group.
    """
    runs = await get_batch_runs(session, org_id=org_id, batch_id=batch_id)
    if not runs:
        return []

    # One grouped query for eval pass rate across the whole batch (no N+1).
    # Guardrail rows are excluded per the eval_results consumer contract.
    run_ids = [run.id for run in runs]
    eval_stats: dict[uuid.UUID, tuple[int, int]] = {}
    er_result = await session.execute(
        select(
            EvalResult.run_id,
            func.count(EvalResult.id),
            func.sum(case((EvalResult.passed, 1), else_=0)),
        )
        .where(EvalResult.run_id.in_(run_ids), non_guardrail_eval_results_clause())
        .group_by(EvalResult.run_id)
    )
    for run_id, total, passed in er_result.all():
        eval_stats[uuid.UUID(str(run_id))] = (int(total or 0), int(passed or 0))

    base_overrides = _as_dict(runs[0].variant_config_snapshot).get("run_context_overrides", {})
    base_overrides = _as_dict(base_overrides)

    entries: list[dict[str, Any]] = []
    for run in runs:
        frozen = _as_dict(run.variant_config_snapshot)
        overrides = _as_dict(frozen.get("run_context_overrides", {}))
        total, passed = eval_stats.get(run.id, (0, 0))
        entries.append(
            {
                "run_id": run.id,
                "run_number": run.run_number,
                "status": run.status,
                "variant_id": frozen.get("variant_id"),
                "variant_name": frozen.get("variant_name") or "unknown",
                "snapshot_id": _coerce_snapshot_id(frozen.get("snapshot_id")),
                "run_context_overrides": overrides,
                "eval_pass_rate": round(passed / total, 4) if total else None,
                "eval_count": total,
                "total_cost_usd": run.total_cost_usd,
                "total_tokens": run.total_tokens,
                "created_at": run.created_at,
                "completed_at": run.completed_at,
                "override_diff": _override_diff(base_overrides, overrides),
            }
        )
    return entries
