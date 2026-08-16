"""CRUD for the org guardrail-config snapshot pin (``guardrail_pins_json``).

The pin is a single JSON document on the ``organisations`` row — the org is
the natural scope of guardrail configuration. This layer is deliberately
storage-only: it moves raw JSON dicts, never ``modulo.core`` domain objects
(the DB layer must not import core — import-linter contract
``db-does-not-import-core``). The route layer (``modulo.api``) owns the
``GuardrailPin`` <-> dict conversion. Reads/writes are scoped to the caller's
``organisation_id``; the route layer sets the RLS context inside the
transaction (``set_rls_org``) before calling these.
"""

from __future__ import annotations

import uuid
from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import NoResultFound
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.eval_definition import EvalDefinition as EvalDefinitionRow
from modulo.db.models.organisation import Organisation


async def load_pipeline_guardrail_rows(
    session: AsyncSession,
    *,
    pipeline_id: uuid.UUID,
    organisation_id: uuid.UUID,
) -> list[EvalDefinitionRow]:
    """Load a pipeline's bound guardrail eval rows (``eval_type='guardrail'``).

    Single shared implementation of the guardrail row-load used by the
    run-creation interception seam (``db.crud.run``), the guardrail-override
    seam (``core.pipeline_engine.recovery``), snapshot guardrail pinning
    (``db.crud.pipeline_snapshot``), and the graph-save cap validation
    (``api.routes.pipelines``) — previously copy-pasted at each call site.
    Returns ORM rows; each caller converts to its own DTO
    (``to_engine_definition`` / ``serialize_guardrail_pin``) so result shapes
    are unchanged. The filter is fixed: ``pipeline_id`` + ``organisation_id`` +
    ``eval_type == 'guardrail'``.
    """
    result = await session.execute(
        select(EvalDefinitionRow).where(
            EvalDefinitionRow.pipeline_id == pipeline_id,
            EvalDefinitionRow.organisation_id == organisation_id,
            EvalDefinitionRow.eval_type == "guardrail",
        )
    )
    return list(result.scalars().all())


async def get_guardrail_pin(session: AsyncSession, org_id: uuid.UUID) -> dict[str, Any] | None:
    """Return the org's raw ``guardrail_pins_json`` document, or None."""
    result = await session.execute(select(Organisation.guardrail_pins_json).where(Organisation.id == org_id))
    return result.scalar_one_or_none()


async def set_guardrail_pin(session: AsyncSession, org_id: uuid.UUID, pin_json: dict[str, Any]) -> None:
    """Persist *pin_json* as the org's ``guardrail_pins_json``.

    Raises ``NoResultFound`` when the org row does not exist (a caller with a
    valid principal always has an org, so this is a defensive signal).
    """
    result = cast(
        "CursorResult[Any]",
        await session.execute(
            update(Organisation).where(Organisation.id == org_id).values(guardrail_pins_json=pin_json)
        ),
    )
    if result.rowcount != 1:
        raise NoResultFound(f"Organisation {org_id} not found for guardrail pin update")


__all__ = ["get_guardrail_pin", "load_pipeline_guardrail_rows", "set_guardrail_pin"]
