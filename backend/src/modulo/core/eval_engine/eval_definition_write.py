"""Shared redirect helper for eval-definition writes (FAR-1101, chunk 3b).

Every write path (REST create/update, MCP create/update, feedback publish)
passes through :func:`create_or_update_eval` which constructs an ``Eval``
row and (where appropriate) a ``PolicyGate`` row, then persists them.

Version stamping (FAR-382) lives inside the helper:
  - create → ``version=1``
  - update (``existing_eval_id`` given) → bump ``version``, snapshot ``pre_version_raw``

The guardrail config-vocabulary validator (:func:`validate_guardrail_request`)
is consolidated here as the single shared validator.  The ``failure_behaviour``
parameter was retired from the public surface in FAR-1103 chunk 5a.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.eval_engine.policy_gate import (
    GUARDRAIL_EVAL_TYPE,
    validate_binding,
)

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Guardrail config-vocabulary validator (consolidated from api/routes/evals.py)
# ---------------------------------------------------------------------------


def validate_guardrail_request(
    *,
    eval_type: str,
    config_json: dict[str, Any] | None,
) -> None:
    """Graph-save validation for guardrail definitions (FAR-208 item 5).

    Three config-vocabulary checks.  The ``failure_behaviour`` parameter was
    retired from the public surface in FAR-1103 chunk 5a.

    1. ``config_json.action`` not in ``("observe", "warn", "block", "redact")`` → reject.
    2. Top-level ``config_json.type`` not in ``("regex", "json_schema")`` → reject.
    3. Nested ``config_json.detection.type`` not in ``("regex", "json_schema")`` → reject.

    Raises :class:`fastapi.HTTPException` (422) on violation.
    """
    if eval_type != "guardrail":
        return

    if config_json is None:
        return

    # 1. Config-vocabulary check on the guardrail action
    action = config_json.get("action")
    if action is not None and action not in ("observe", "warn", "block", "redact"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Guardrail action must be one of observe|warn|block|redact (got {action!r}).",
        )

    # 2. Top-level detection type check
    detection_type = config_json.get("type")
    if detection_type is not None and detection_type not in ("regex", "json_schema"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Guardrail detection must be regex|json_schema (got {detection_type!r}).",
        )

    # 3. Nested detection-envelope type check (PRD §8.17)
    envelope = config_json.get("detection")
    if isinstance(envelope, dict):
        env_type = envelope.get("type")
        if env_type is not None and env_type not in ("regex", "json_schema"):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Guardrail detection envelope type must be regex|json_schema (got {env_type!r}).",
            )


# ---------------------------------------------------------------------------
# Shared redirect helper
# ---------------------------------------------------------------------------


async def create_or_update_eval(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    account_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    node_id: uuid.UUID | None,
    name: str,
    eval_type: str,
    config_json: dict[str, Any],
    failure_behaviour: str,
    pass_threshold: float | None,
    suite_id: str | None,
    eval_suite_id: uuid.UUID | None = None,
    existing_eval_id: uuid.UUID | None = None,
) -> Any:
    """Persist an ``Eval`` row and optionally a ``PolicyGate`` row.

    Branches on eval type BEFORE calling ``validate_binding`` (R5):

    1. ``eval_type == 'guardrail'`` → config-vocabulary validator only;
       persist ``Eval`` row only (no ``PolicyGate``, never call ``validate_binding``).
    2. Else ``node_id is not None`` → call ``validate_binding``;
       on ``PolicyGateBindingViolationError`` re-raise for the caller.
       Persist ``Eval`` + ``PolicyGate(action=failure_behaviour)``.
    3. Else (suite-scoped non-guardrail) → persist ``Eval`` row only
       (no ``PolicyGate``).

    ``failure_behaviour`` is internal — callers always pass ``"warn"``
    since FAR-1103 chunk 5a retired the public surface.

    Version stamping lives inside the helper:
      - create → ``version=1``
      - update (``existing_eval_id`` given) → bump ``version``, snapshot ``pre_version_raw``.

    Returns the persisted ``Eval`` row instance.
    """
    from modulo.db.models.eval import Eval
    from modulo.db.models.policy_gate import PolicyGate

    # --- Branch on eval type BEFORE calling validate_binding ---

    if eval_type == GUARDRAIL_EVAL_TYPE:
        # Branch 1: guardrail — config-vocabulary validator only, no PolicyGate
        validate_guardrail_request(
            eval_type=eval_type,
            config_json=config_json,
        )
        pg_action = None

    elif node_id is not None:
        # Branch 2: node-scoped non-guardrail — call validate_binding
        pg_id = existing_eval_id or uuid.uuid4()
        pg_fields = {
            "id": pg_id,
            "organisation_id": org_id,
            "node_id": node_id,
        }
        ev_fields = {
            "id": existing_eval_id or uuid.uuid4(),
            "organisation_id": org_id,
            "node_id": node_id,
            "eval_type": eval_type,
        }
        validate_binding(pg_fields, ev_fields)
        pg_action = failure_behaviour or "warn"

    else:
        # Branch 3: suite-scoped non-guardrail — no PolicyGate
        pg_action = None

    # --- Eval row persistence ---

    if existing_eval_id is not None:
        # UPDATE path — load existing, snapshot, bump version
        existing = (
            await session.execute(
                select(Eval).where(
                    Eval.id == existing_eval_id,
                    Eval.organisation_id == org_id,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            # Eval row exists (backfilled) — update it
            existing.pre_version_raw = {"config_json": existing.config_json}
            existing.version = (existing.version or 1) + 1
            existing.pipeline_id = pipeline_id
            existing.node_id = node_id
            existing.name = name
            existing.eval_type = eval_type
            existing.config_json = config_json
            existing.pass_threshold = pass_threshold  # type: ignore[assignment]
            existing.suite_id = suite_id
            if eval_suite_id is not None:
                existing.eval_suite_id = eval_suite_id
            await session.flush()
            eval_row = existing
        else:
            # Eval row does not exist yet (first-time redirect) — create it
            eval_row = Eval(
                id=existing_eval_id,
                organisation_id=org_id,
                pipeline_id=pipeline_id,
                node_id=node_id,
                name=name,
                eval_type=eval_type,
                config_json=config_json,
                pass_threshold=pass_threshold,
                suite_id=suite_id,
                eval_suite_id=eval_suite_id,
                account_id=account_id,
                version=1,
            )
            session.add(eval_row)
            await session.flush()
    else:
        # CREATE path — version=1
        eval_row = Eval(
            organisation_id=org_id,
            pipeline_id=pipeline_id,
            node_id=node_id,
            name=name,
            eval_type=eval_type,
            config_json=config_json,
            pass_threshold=pass_threshold,
            suite_id=suite_id,
            eval_suite_id=eval_suite_id,
            account_id=account_id,
            version=1,
        )
        session.add(eval_row)
        await session.flush()

    # --- PolicyGate persistence (branches 2 only) ---

    if pg_action is not None and node_id is not None and eval_type != GUARDRAIL_EVAL_TYPE:
        # Upsert PolicyGate for this eval
        existing_gate = (
            await session.execute(
                select(PolicyGate).where(
                    PolicyGate.eval_id == eval_row.id,
                    PolicyGate.organisation_id == org_id,
                    PolicyGate.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()

        if existing_gate is not None:
            # Update existing gate
            existing_gate.action = pg_action
            existing_gate.node_id = node_id
        else:
            # Create new gate
            gate = PolicyGate(
                organisation_id=org_id,
                eval_id=eval_row.id,
                node_id=node_id,
                action=pg_action,
                version=1,
            )
            session.add(gate)

        await session.flush()

    return eval_row
