"""Seed the default cost components for EVERY org (startup AND org creation).

Seeds applied to every org at startup; the caller also invokes this on org
creation. Idempotent: insert only when no ACTIVE row has the ``name``;
soft-deleted names are skipped (a soft-deleted seed name is not re-created).

ORG ENUMERATION runs in SYSTEM CONTEXT with NO ``set_rls_org`` — exactly like
the probe's org enumeration. If the enumeration were subject to RLS it would
return zero orgs and SILENTLY skip seeding. ``set_rls_org`` applies ONLY to the
per-org seed inserts, inside each ``session.begin()`` (or the inserts are
RLS-filtered). Each org is wrapped in ``try/except`` so ONE org's RLS/DB
failure cannot abort the whole seed.
"""

from __future__ import annotations

import logging
import uuid
from typing import TypedDict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from modulo.db.models.cost_component import CostComponent, CostComponentKind
from modulo.db.models.organisation import Organisation
from modulo.db.rls import set_rls_org

_log = logging.getLogger(__name__)


class _ComponentSeed(TypedDict):
    name: str
    display_name: str
    kind: str
    formula: str | None
    rate_usd: None
    rate_fallback: str | None
    report_key: str | None
    sort_order: int


# name -> (kind, formula, rate_usd, rate_fallback, report_key, sort_order)
DEFAULT_COST_COMPONENTS: list[_ComponentSeed] = [
    {
        "name": "llm_tokens",
        "display_name": "LLM Tokens",
        "kind": CostComponentKind.CALCULATED.value,
        "formula": "tokens_input * input_token_rate + tokens_output * output_token_rate",
        "rate_usd": None,
        "rate_fallback": None,
        "report_key": None,
        "sort_order": 10,
    },
    {
        "name": "sandbox_infra",
        "display_name": "Sandbox Infrastructure",
        "kind": CostComponentKind.CALCULATED.value,
        "formula": "rate * wall_clock_hours",
        "rate_usd": None,
        "rate_fallback": "e2b_rate",
        "report_key": None,
        "sort_order": 20,
    },
    {
        "name": "model_tokens",
        "display_name": "Model cost (self-reported)",
        "kind": CostComponentKind.SELF_REPORTED.value,
        "formula": None,  # NULL — implicit ``reported``
        "rate_usd": None,
        "rate_fallback": None,
        "report_key": "model_cost_usd",
        "sort_order": 30,
    },
]


async def seed_cost_components_for_org(session: AsyncSession, org_id: uuid.UUID | None) -> None:
    """Seed the default components for a single org (idempotent)."""
    await set_rls_org(session, org_id)
    for spec in DEFAULT_COST_COMPONENTS:
        existing = await session.execute(
            select(CostComponent).where(
                CostComponent.organisation_id == org_id,
                CostComponent.name == spec["name"],
                CostComponent.deleted_at.is_(None),
            )
        )
        if existing.scalar_one_or_none() is not None:
            continue
        session.add(
            CostComponent(
                organisation_id=org_id,
                name=spec["name"],
                display_name=spec["display_name"],
                kind=spec["kind"],
                formula=spec["formula"],
                rate_usd=spec["rate_usd"],
                rate_fallback=spec["rate_fallback"],
                report_key=spec["report_key"],
                enabled=True,
                sort_order=spec["sort_order"],
            )
        )
        _log.info("cost_components.seeded", extra={"org_id": str(org_id), "name": spec["name"]})


async def seed_cost_components(factory: async_sessionmaker[AsyncSession]) -> int:
    """Seed default components for every org. Returns the org count seeded.

    Org enumeration runs in SYSTEM CONTEXT (NO set_rls_org) — the app role owns
    ``organisations`` so a plain query sees all rows. ``set_rls_org`` applies
    ONLY to the per-org seed inserts.
    """
    async with factory() as session, session.begin():
        org_result = await session.execute(select(Organisation.id).order_by(Organisation.created_at))
        org_ids = [row[0] for row in org_result.all()]

    print(f"SEED_COST_COMPONENTS: enumerated orgs={len(org_ids)}", flush=True)  # noqa: T201
    if not org_ids:
        print("SEED_COST_COMPONENTS: NO ORGS — enumeration returned zero", flush=True)  # noqa: T201

    seeded = 0
    for org_id in org_ids:
        try:
            async with factory() as session, session.begin():
                await seed_cost_components_for_org(session, org_id)
            print(f"SEED_COST_COMPONENTS: org {org_id} OK", flush=True)  # noqa: T201
            seeded += 1
        except Exception as e:
            _log.exception("cost_components.seed_org_failed", extra={"org_id": str(org_id)})
            print(f"SEED_COST_COMPONENTS: org {org_id} FAILED: {e!r}", flush=True)  # noqa: T201
    print(f"SEED_COST_COMPONENTS: complete seeded={seeded} of orgs={len(org_ids)}", flush=True)  # noqa: T201
    _log.info("cost_components.seed_complete", extra={"orgs": seeded})
    return seeded
