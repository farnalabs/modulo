"""Materialise the org's applied guardrail config onto a pipeline's rows (FAR-1593).

The org-level guardrail default is stored **per pipeline**: ``POST
/api/v1/guardrails/config/{apply,import}`` fans the config set out over every
pipeline that exists at that moment (``api/routes/guardrail_config.py``), and
every enforcement surface — the run-creation interception pass
(``db/crud/run.py``), the webhook-intake pre-trigger pass
(``core/trigger_engine/pre_guardrail.py``), snapshot pinning — loads rows
**by pipeline id**.

Before FAR-1593 the fan-out ran only at apply/import time, so a pipeline
created afterwards carried no org-level rows until the config was re-applied:
its runs were simply unguarded while ``GET /guardrails/config/drift`` reported
``clean`` (the drift hash deduplicates org-level rows *by name across
pipelines*, so one covered pipeline masks any number of uncovered ones).

This module is the single writer of that materialisation:

* :func:`upsert_org_guardrail_rows` — the per-pipeline upsert both apply/import
  (via the route's ``_apply_guardrail_upserts`` wrapper) and creation-time
  seeding use, so the two paths can never diverge on how a row is built;
* :func:`seed_org_guardrail_rows_for_pipeline` — invoked by
  ``db.crud.pipeline.create_pipeline`` (and the clone path) immediately after
  the new pipeline row flushes, in the SAME transaction. One chokepoint covers
  every ORM creation path (REST, MCP, templates, onboarding, library install,
  workflow import, clone); a raw SQL insert bypasses the ORM seam by design and
  is not a production creation path.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.eval_engine.eval_definition_write import create_or_update_eval
from modulo.core.guardrails.config import (
    GuardrailConfigError,
    GuardrailConfigItem,
    GuardrailConfigSet,
    GuardrailPin,
    load_config_set,
    to_eval_config,
)
from modulo.db.models.eval import Eval

_log = logging.getLogger(__name__)


async def upsert_org_guardrail_rows(
    session: AsyncSession,
    *,
    pipeline_id: uuid.UUID,
    rows_by_name: dict[str, Eval],
    proposed_by_id: dict[str, GuardrailConfigItem],
    config_set: GuardrailConfigSet,
    org_id: uuid.UUID,
    account_id: uuid.UUID,
) -> int:
    """Add missing guardrail rows and upsert the ones config-as-code owns.

    For **new** guardrail items: creates an ``Eval`` row via the shared helper
    with ``eval_type='guardrail'`` and ``node_id=None``. The helper's guardrail
    branch runs the config-vocabulary validator and persists the ``Eval`` row
    only — no ``PolicyGate``, no ``validate_binding`` call.

    For **existing** rows: updates ``config_json`` via the shared helper with
    ``existing_eval_id`` set, so version-stamping is handled inside the helper
    (bumps ``version``, snapshots ``pre_version_raw``). A row bound to a node
    (``node_id`` set) is NOT config-as-code's to own and is left untouched.

    Returns the number of rows created (0 when everything already existed).
    """
    created = 0
    for gid, item in proposed_by_id.items():
        row = rows_by_name.get(gid)
        config_json = to_eval_config(
            item,
            max_guardrails_per_node=config_set.max_guardrails_per_node,
            guardrail_timeout_seconds=config_set.guardrail_timeout_seconds,
        )
        if row is None:
            # New guardrail — create via shared helper (W7)
            await create_or_update_eval(
                session,
                org_id=org_id,
                account_id=account_id,
                pipeline_id=pipeline_id,
                node_id=None,
                name=gid,
                eval_type="guardrail",
                config_json=config_json,
                failure_behaviour="warn",
                pass_threshold=None,
                suite_id=None,
            )
            created += 1
        elif row.node_id is None:
            # Existing guardrail owned by config-as-code — update via shared
            # helper with existing_eval_id so version-stamping is not skipped.
            await create_or_update_eval(
                session,
                org_id=org_id,
                account_id=account_id,
                pipeline_id=pipeline_id,
                node_id=None,
                name=gid,
                eval_type="guardrail",
                config_json=config_json,
                failure_behaviour="warn",
                pass_threshold=None,
                suite_id=None,
                existing_eval_id=row.id,
            )
    return created


async def seed_org_guardrail_rows_for_pipeline(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    account_id: uuid.UUID,
) -> int:
    """Materialise the org's APPLIED guardrail snapshot onto *pipeline_id*.

    Called from ``db.crud.pipeline`` right after a new (or cloned) pipeline row
    flushes, inside the caller's transaction — the pipeline and its guardrail
    rows commit atomically, so a run can never start against an unguarded
    pipeline that should have inherited the org default.

    No applied config (no pin, no snapshot, an empty set) is a fast no-op —
    one PK read of the organisation row. A stored snapshot the config parser
    rejects FAILS CLOSED (raises, rolling back the caller's transaction) so an
    unguarded pipeline is never committed while an applied pin exists: silently
    degrading to zero rows here reproduces exactly the gap FAR-1593 closes
    (runs unguarded while ``GET /guardrails/config/drift`` reads ``clean``),
    and the standing principle is that security paths fail closed. This
    fail-closed rule covers the snapshot only: a stored pin that is not a JSON
    object degrades to a warning-and-no-op (0 rows; the pipeline commits,
    unguarded) rather than raising, because the app writes only dict pins via
    ``set_guardrail_pin`` — a non-dict value is reachable only by out-of-band DB
    corruption and cannot be a pin the app produced. The fail-closed error
    surfaces as HTTP 422 out of ``create_pipeline`` / ``_clone_pipeline_config``
    — the same seam and status the adjacent ``create_or_update_eval`` →
    ``validate_guardrail_request`` guardrail validation uses — and MCP's
    ``create_pipeline`` tool maps it to ``validation_failed`` with the same
    detail. The pin is never rewritten here; an admin must re-apply the config.

    Returns the number of guardrail rows created (0 when nothing was seeded).
    """
    from modulo.db.crud.guardrail_config import get_guardrail_pin, load_pipeline_guardrail_rows

    pin_json = await get_guardrail_pin(session, org_id)
    if pin_json is None:
        return 0
    if not isinstance(pin_json, dict):
        # The column is JSONB (dict | None). A non-dict read must never reach
        # the YAML parser, and it must not fail silently either — say so.
        _log.warning("guardrail.seed.pin_not_object", extra={"org_id": str(org_id)})
        return 0
    pin = GuardrailPin.from_json(org_id, pin_json)
    if pin is None or not pin.serialized_snapshot:
        return 0
    try:
        config_set = load_config_set(pin.serialized_snapshot)
    except GuardrailConfigError as exc:
        # Fail closed (FAR-1593 QA): an applied pin whose snapshot no longer
        # parses means the org's safety controls cannot be materialised onto
        # the new pipeline. Committing the pipeline with zero guardrail rows
        # would silently reproduce the unguarded-pipeline gap this module
        # exists to close — the drift surface would still read clean because
        # the drift hash deduplicates org-level rows by name across pipelines.
        # The caller's transaction rolls back, so no unguarded pipeline is
        # ever committed. Typed as the same HTTP 422 the adjacent
        # ``create_or_update_eval`` → ``validate_guardrail_request`` path
        # already raises out of ``create_pipeline`` (REST passthrough via
        # ``handle_db_errors``; MCP ``create_pipeline`` maps it to
        # ``validation_failed``).
        _log.exception(
            "guardrail.seed.invalid_applied_snapshot",
            extra={"org_id": str(org_id)},
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                "The applied guardrail snapshot is unreadable; an admin must "
                "re-apply the config (POST /api/v1/guardrails/config/apply) "
                "before pipelines can be created or cloned."
            ),
        ) from exc
    if not config_set.guardrails:
        return 0

    rows = await load_pipeline_guardrail_rows(
        session,
        pipeline_id=pipeline_id,
        organisation_id=org_id,
    )
    rows_by_name = {row.name: row for row in rows if row.name is not None}
    proposed_by_id = {item.id: item for item in config_set.guardrails}
    created = await upsert_org_guardrail_rows(
        session,
        pipeline_id=pipeline_id,
        rows_by_name=rows_by_name,
        proposed_by_id=proposed_by_id,
        config_set=config_set,
        org_id=org_id,
        account_id=account_id,
    )
    if created:
        await session.flush()
        _log.info(
            "guardrail.seed.materialised",
            extra={"org_id": str(org_id), "pipeline_id": str(pipeline_id), "rows_created": created},
        )
    return created


__all__ = ["seed_org_guardrail_rows_for_pipeline", "upsert_org_guardrail_rows"]
