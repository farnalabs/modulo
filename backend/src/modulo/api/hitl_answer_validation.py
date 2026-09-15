"""Shared HITL answer validation (FAR-860).

Single source of truth for validating a ``kind`` answer dict against the
gate's declared ``response_contract.kind``.  Both the REST route
(``_validate_choice_answer``) and the MCP tool call
(``_validate_mcp_choice_answer``) delegate here so validation rules never
drift.

Design
------
``validate_hitl_answer`` resolves the gate's response contract first, then
applies kind-specific rules:

* **choice** — ``option_id`` is required, must be a non-empty string, and
  must appear in the declared option set.
* **approval** (or no ``response_contract``) — ``option_id`` is rejected;
  only ``kind: approval`` is accepted.
* **unknown kind** — rejected.

Callers adapt the ``ValueError`` message into their own error format (HTTP
422 for REST, ``{"error": ...}`` for MCP).
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.hitl_gate_config import resolve_hitl_gate_config


class AnswerValidationError(ValueError):
    """Raised when a HITL answer fails validation against the gate contract."""


async def validate_hitl_answer(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    gate_id: str,
    org_id: uuid.UUID,
    answer: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Validate *answer* against the gate's ``response_contract``.

    Returns the validated answer dict, or ``None`` when no answer is provided.
    Raises ``AnswerValidationError`` when validation fails.
    """
    if answer is None:
        return None

    kind = answer.get("kind")
    if not isinstance(kind, str) or not kind:
        raise AnswerValidationError("answer must have a non-empty 'kind' string")

    config = await resolve_hitl_gate_config(
        session,
        run_id=run_id,
        gate_id=gate_id,
        org_id=org_id,
    )

    if config is None:
        # Config unresolvable (legacy snapshot / graph drift): fail-open.
        return answer

    rc = config.get("response_contract")
    if not isinstance(rc, dict):
        # No contract declared: the gate is approval-type.
        if kind != "approval":
            raise AnswerValidationError(f"gate has no response_contract; answer kind must be 'approval', got {kind!r}")
        # Reject option_id on a no-contract (approval) gate.
        option_id = answer.get("option_id")
        if isinstance(option_id, str) and option_id:
            raise AnswerValidationError("option_id is not permitted on an approval gate")
        return answer

    declared_kind = rc.get("kind")
    if kind != declared_kind:
        raise AnswerValidationError(
            f"answer kind {kind!r} does not match gate response_contract kind {declared_kind!r}"
        )

    if kind == "choice":
        option_id = answer.get("option_id")
        if not isinstance(option_id, str) or not option_id:
            raise AnswerValidationError("answer must have a non-empty 'option_id' string")
        options = rc.get("options")
        if not isinstance(options, list):
            raise AnswerValidationError("gate response_contract has no options")
        valid_ids = {opt.get("id") for opt in options if isinstance(opt, dict)}
        if option_id not in valid_ids:
            raise AnswerValidationError(
                f"option_id {option_id!r} is not a valid option for this gate; valid ids: {sorted(valid_ids)}"
            )
    elif kind == "approval":
        # Reject option_id on an approval gate (belt-and-suspenders with the
        # answer-kind mismatch check — an attacker sending an arbitrary
        # option_id on an approval gate would bypass the mismatch check but
        # must not succeed here).
        option_id = answer.get("option_id")
        if isinstance(option_id, str) and option_id:
            raise AnswerValidationError("option_id is not permitted on an approval gate")
    else:
        raise AnswerValidationError(f"unsupported answer kind {kind!r}; expected 'choice' or 'approval'")

    return answer
