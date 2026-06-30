"""CRUD for VariantGroup — A/B test variant management.

All functions require RLS org context to be set by the caller.
"""

import random
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.run import count_active_runs_for_pipeline, create_run
from modulo.db.models.variant_group import VariantGroup


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


async def get_variant_group(session: AsyncSession, group_id: uuid.UUID) -> VariantGroup | None:
    result = await session.execute(select(VariantGroup).where(VariantGroup.id == group_id))
    return result.scalar_one_or_none()


async def list_variant_groups(
    session: AsyncSession,
    *,
    pipeline_id: uuid.UUID | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[VariantGroup], int]:
    q = select(VariantGroup)
    count_q = select(func.count()).select_from(VariantGroup)
    if pipeline_id is not None:
        q = q.where(VariantGroup.pipeline_id == pipeline_id)
        count_q = count_q.where(VariantGroup.pipeline_id == pipeline_id)

    offset = (page - 1) * page_size
    total = (await session.execute(count_q)).scalar_one()
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


async def delete_variant_group(session: AsyncSession, group_id: uuid.UUID) -> bool:
    group = await get_variant_group(session, group_id)
    if group is None:
        return False
    await session.delete(group)
    await session.flush()
    return True


async def increment_run_count(session: AsyncSession, group_id: uuid.UUID) -> VariantGroup | None:
    group = await get_variant_group(session, group_id)
    if group is None:
        return None
    group.run_count = (group.run_count or 0) + 1
    await session.flush()
    return group


async def check_pipeline_run_quota(session: AsyncSession, group: VariantGroup) -> bool:
    """Check if the pipeline is within its concurrent run quota.

    Returns True if a new run is allowed, False if quota is exceeded.
    """
    active = await count_active_runs_for_pipeline(session, group.pipeline_id)
    return active < group.max_concurrent_runs


def pick_variant_weighted(
    variants: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Select a variant using weighted random selection.

    Each variant dict should contain a 'weight' key (default 1.0).
    If only one variant, returns it directly (short-circuit).
    """
    if not variants:
        return None
    if len(variants) == 1:
        return variants[0]

    weights = [float(v.get("weight", 1.0)) for v in variants]
    total = sum(weights)
    if total <= 0:
        return random.choice(variants)  # noqa: S311 — variant selection is not cryptographic

    r = random.random() * total  # noqa: S311 — variant selection is not cryptographic
    cumulative = 0.0
    for i, w in enumerate(weights):
        cumulative += w
        if r <= cumulative:
            return variants[i]
    return variants[-1]


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
    """
    if not await check_pipeline_run_quota(session, group):
        return None

    variant = pick_variant_weighted(group.variants)
    if variant is None:
        return None

    merged_payload = dict(input_payload or {})
    overrides = variant.get("run_context_overrides", {})
    if isinstance(overrides, dict):
        merged_payload.update(overrides)

    if group.degraded_evals:
        merged_payload["_degraded_evals"] = True

    run = await create_run(
        session,
        org_id=org_id,
        pipeline_id=group.pipeline_id,
        snapshot_id=uuid.UUID(str(variant["snapshot_id"]))
        if isinstance(variant.get("snapshot_id"), str)
        else variant["snapshot_id"],
        trigger_type=trigger_type,
        input_payload=merged_payload,
        account_id=account_id,
    )

    await increment_run_count(session, group.id)

    return {
        "run_id": run.id,
        "variant": variant,
        "merged_payload": merged_payload,
    }


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
        from modulo.db.models.eval_definition import EvalDefinition

        result = await session.execute(select(EvalDefinition).where(EvalDefinition.pipeline_id == group.pipeline_id))
        eval_defs = list(result.scalars())
        eval_def_ids = [e.id for e in eval_defs]

    gaps: list[dict[str, Any]] = []
    for variant in group.variants:
        variant_eval_ids = set(uuid.UUID(str(eid)) for eid in variant.get("eval_definition_ids", []))
        missing = [str(eid) for eid in eval_def_ids if eid not in variant_eval_ids]
        if missing:
            gaps.append(
                {
                    "variant": variant,
                    "missing_evals": missing,
                }
            )
    return gaps


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

    from modulo.db.models.pipeline_snapshot import PipelineSnapshot

    result = await session.execute(select(PipelineSnapshot).where(PipelineSnapshot.id.in_(snapshot_ids)))
    snapshots = {s.id: s for s in result.scalars()}

    base_variants = [v for v in group.variants if uuid.UUID(str(v.get("snapshot_id", ""))) in (base_snapshot_ids or [])]
    comparison_variants = [
        v for v in group.variants if uuid.UUID(str(v.get("snapshot_id", ""))) not in (base_snapshot_ids or [])
    ]

    diffs: list[dict[str, Any]] = []
    for cv in comparison_variants:
        cv_id = uuid.UUID(str(cv["snapshot_id"]))
        cv_snapshot = snapshots.get(cv_id)
        for bv in base_variants:
            bv_id = uuid.UUID(str(bv["snapshot_id"]))
            bv_snapshot = snapshots.get(bv_id)
            if cv_snapshot is None or bv_snapshot is None:
                continue

            bv_pins = {p["agent_id"]: p["prompt_version_hash"] for p in bv_snapshot.prompt_pins_json}
            cv_pins = {p["agent_id"]: p["prompt_version_hash"] for p in cv_snapshot.prompt_pins_json}

            agent_diffs = []
            for agent_id, cv_hash in cv_pins.items():
                bv_hash = bv_pins.get(agent_id)
                if bv_hash and bv_hash != cv_hash:
                    agent_diffs.append(
                        {
                            "agent_id": agent_id,
                            "base_hash": bv_hash,
                            "variant_hash": cv_hash,
                        }
                    )

            if agent_diffs:
                diffs.append(
                    {
                        "base_variant": bv,
                        "variant": cv,
                        "agent_diffs": agent_diffs,
                    }
                )

    return diffs
