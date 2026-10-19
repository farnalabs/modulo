"""Shared redirect helper for eval-definition writes (FAR-1101, chunk 3b).

Every write path (REST create/update, MCP create/update, feedback publish)
passes through :func:`create_or_update_eval` which constructs an ``Eval``
row and (where appropriate) a ``PolicyGate`` row, then persists them.

Version stamping (FAR-382) lives inside the helper:
  - create → ``version=1``
  - update (``existing_eval_id`` given) → bump ``version``, snapshot ``pre_version_raw``

The guardrail config-vocabulary validator (:func:`validate_guardrail_request`)
is consolidated here as the single shared validator; the FAR-209 Track B
binding guard (:func:`_reject_unbound_output_point_guardrail`) sits beside it
so EVERY authoring path (REST, MCP, feedback publish, config-as-code) fails
closed on an unenforceable output-point row.  The ``failure_behaviour``
parameter was retired from the public surface in FAR-1103 chunk 5a.
"""

from __future__ import annotations

import dataclasses
import logging
import uuid
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.eval_engine.policy_gate import (
    validate_binding,
)

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Guardrail config-vocabulary validator (consolidated from api/routes/evals.py)
# ---------------------------------------------------------------------------


def _reject_invalid_action(config_json: dict[str, Any]) -> None:
    """Reject a guardrail action outside the config vocabulary."""
    action = config_json.get("action")
    if action is not None and action not in ("observe", "warn", "block", "redact"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Guardrail action must be one of observe|warn|block|redact (got {action!r}).",
        )


def _reject_invalid_interception_point(config_json: dict[str, Any]) -> None:
    """Reject an unknown interception-point seam (FAR-209 Track B).

    An unknown seam value must never silently bind — the engine's
    input-passthrough/output-strict split would strand it.
    """
    interception_point = config_json.get("interception_point")
    if interception_point is not None and interception_point not in ("input", "output"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(f"Guardrail interception_point must be one of input|output (got {interception_point!r})."),
        )


def _reject_invalid_detection_type(config_json: dict[str, Any]) -> None:
    """Reject a top-level detection type outside regex|json_schema."""
    detection_type = config_json.get("type")
    if detection_type is not None and detection_type not in ("regex", "json_schema"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Guardrail detection must be regex|json_schema (got {detection_type!r}).",
        )


def _reject_invalid_detection_envelope(config_json: dict[str, Any]) -> None:
    """Reject a nested detection-envelope type outside regex|json_schema (PRD §8.17)."""
    envelope = config_json.get("detection")
    if isinstance(envelope, dict):
        env_type = envelope.get("type")
        if env_type is not None and env_type not in ("regex", "json_schema"):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Guardrail detection envelope type must be regex|json_schema (got {env_type!r}).",
            )


def _reject_destroy_without_pepper(config_json: dict[str, Any]) -> None:
    """Reject a destroy-redaction rule with no configured deployment pepper (FAR-209 Track B).

    The config-as-code propose path enforces this (``_validate_config_item``)
    but the graph-save authoring paths did not, so a destroy rule could be
    authored with the pepper unset and only fail (closed) at the runtime edge.
    Reject at authoring here too. Checked regardless of ``action`` (mirroring
    ``_validate_config_item``); a non-list ``redaction`` is left to shape-level
    validation elsewhere.
    """
    redaction = config_json.get("redaction")
    if isinstance(redaction, list):
        from modulo.core.guardrails import FieldRedactionMode
        from modulo.core.guardrails.redaction_mode import resolve_guardrail_destroy_pepper

        if (
            any(isinstance(rule, dict) and rule.get("mode") == FieldRedactionMode.DESTROY for rule in redaction)
            and not resolve_guardrail_destroy_pepper()
        ):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=(
                    "Guardrail destroy redaction mode requires the guardrail_destroy_pepper setting to be configured."
                ),
            )


def validate_guardrail_request(
    *,
    eval_type: str,
    config_json: dict[str, Any] | None,
) -> None:
    """Graph-save validation for guardrail definitions (FAR-208 item 5).

    Five checks.  The ``failure_behaviour`` parameter was retired from the
    public surface in FAR-1103 chunk 5a.

    1. ``config_json.action`` not in ``("observe", "warn", "block", "redact")`` → reject.
    2. Top-level ``config_json.type`` not in ``("regex", "json_schema")`` → reject.
    3. Nested ``config_json.detection.type`` not in ``("regex", "json_schema")`` → reject.
    4. ``config_json.interception_point`` (FAR-209 Track B) not in
       ``("input", "output")`` → reject (an unknown seam value must never
       silently bind — the engine's input-passthrough/output-strict split
       would strand it).
    5. A ``destroy`` redaction rule with no configured deployment pepper
       (FAR-209 Track B) → reject, mirroring the config-as-code propose path
       (``_validate_config_item``) so the graph-save authoring paths fail
       closed at authoring time too.

    Raises :class:`fastapi.HTTPException` (422) on violation.
    """
    if eval_type != "guardrail":
        return

    if config_json is None:
        return

    _reject_invalid_action(config_json)
    _reject_invalid_interception_point(config_json)
    _reject_invalid_detection_type(config_json)
    _reject_invalid_detection_envelope(config_json)
    _reject_destroy_without_pepper(config_json)


# ---------------------------------------------------------------------------
# Shared redirect helper
# ---------------------------------------------------------------------------


def _reject_unbound_output_point_guardrail(
    *,
    node_id: uuid.UUID | None,
    config_json: dict[str, Any] | None,
) -> None:
    """Fail closed on an UNBOUND output-point guardrail (FAR-209 Track B).

    An ``interception_point == "output"`` guardrail is enforced ONLY at the
    executor's node-output seam, which loads node-scoped evals
    (``Eval.node_id IS NOT NULL``) and is keyed per node in
    ``eval_definitions_by_node``. A pipeline-level (``node_id IS NULL``) row:

    * is excluded from the ingestion-edge pass by
      ``exclude_output_interception_definitions`` (the input pass must not
      see an output-point def), and
    * is never loaded by the output seam (it has no node to bind to),

    so a block/redact OUTPUT guardrail authored pipeline-level would be
    **silently unenforced** — a security hole with no error anywhere. Reject
    at authoring with a clear 422 instead: the operator binds it to a node or
    moves it to the input seam.

    ``interception_point`` values other than the literal ``"output"``
    (including unknown ones) are deliberately NOT rejected here — they stay on
    the ingestion pass, whose strict validation fails closed on them.
    """
    point = (config_json or {}).get("interception_point")
    if node_id is None and point == "output":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                "Guardrail interception_point='output' requires a node binding: an "
                "output-point guardrail is enforced only at the node-output seam, so a "
                "pipeline-level (unbound, node_id=null) row would never fire. Bind it to "
                "a node, or set interception_point='input' to enforce it at the "
                "ingestion edge."
            ),
        )


@dataclasses.dataclass(frozen=True)
class EvalWriteOptions:
    """Optional write-path modifiers for :func:`create_or_update_eval`.

    Groups the three trailing optional arguments (S107 — the helper had 14
    parameters, above the 13 authorized). Semantics are unchanged from the
    former keyword parameters:

      * ``eval_suite_id`` — optional FK onto the owning eval suite; ``None``
        leaves the row's ``eval_suite_id`` untouched on update / unset on
        create.
      * ``existing_eval_id`` — when set, the helper takes the UPDATE path
        (load, snapshot ``pre_version_raw``, bump ``version``) instead of
        CREATE; a missing row falls back to a first-time create with that id.
      * ``evaluation_mode`` — FAR-939 chunk 12 §3.1 preserve-or-default:
        ``None`` means *preserve the live gate's mode on update* /
        *default to ``automatic`` on create*; a non-``None`` value is the
        explicit write.
    """

    eval_suite_id: uuid.UUID | None = None
    existing_eval_id: uuid.UUID | None = None
    evaluation_mode: str | None = None


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
    write_options: EvalWriteOptions | None = None,
) -> Any:
    """Persist an ``Eval`` row and optionally a ``PolicyGate`` row.

    Branches on eval type BEFORE calling ``validate_binding`` (R5):

    1. ``eval_type == 'guardrail'`` → config-vocabulary validator +
       :func:`_reject_unbound_output_point_guardrail` (FAR-209 Track B:
       reject ``interception_point='output'`` with ``node_id IS NULL`` — such
       a row is enforced by no seam and would fire silently never);
       persist ``Eval`` row only (no ``PolicyGate``, never call ``validate_binding``).
    2. Else ``node_id is not None`` → call ``validate_binding``;
       on        ``PolicyGateBindingViolationError`` re-raise for the caller.
       Persist ``Eval`` + ``PolicyGate(action=failure_behaviour,
       evaluation_mode=<see the preserve-or-default rule below>)``.
    3. Else (suite-scoped non-guardrail) → persist ``Eval`` row only
       (no ``PolicyGate``).

    ``failure_behaviour`` is internal — callers always pass ``"warn"``
    since FAR-1103 chunk 5a retired the public surface.

    ``write_options.evaluation_mode`` (FAR-939 chunk 12) is persisted on
    BOTH gate upsert paths, with preserve-or-default semantics (§3.1 —
    every update surface must carry OR preserve the field):

      - **CREATE** (no live gate): ``evaluation_mode or "automatic"`` — a
        fresh gate is automatic (current behaviour) unless the caller
        passes a mode explicitly.
      - **UPDATE** (live gate): assigned ONLY when the caller passes a
        non-``None`` value; ``None`` means *preserve the live gate's
        mode*. This is what makes an MCP (or any partial) eval edit
        unable to silently reset a ``human`` gate to ``automatic`` —
        mirroring ``failure_behaviour`` preservation.

    REST callers forward the live value; callers that do not carry the
    field pass ``None`` and preserve on update / default on create.

    Version stamping lives inside the helper:
      - create → ``version=1``
      - update (``write_options.existing_eval_id`` given) → bump ``version``,
        snapshot ``pre_version_raw``.

    The three optional modifiers ride on ``write_options`` (an
    :class:`EvalWriteOptions`) rather than as bare keyword parameters —
    same semantics, one fewer parameter (S107).

    Returns the persisted ``Eval`` row instance.
    """
    opts = write_options if write_options is not None else EvalWriteOptions()
    eval_suite_id = opts.eval_suite_id
    existing_eval_id = opts.existing_eval_id
    evaluation_mode = opts.evaluation_mode

    # --- Branch on eval type BEFORE calling validate_binding ---
    pg_action = _resolve_pg_action(
        eval_type=eval_type,
        org_id=org_id,
        node_id=node_id,
        existing_eval_id=existing_eval_id,
        failure_behaviour=failure_behaviour,
        config_json=config_json,
    )

    # --- Eval row persistence ---
    eval_row = await _persist_eval_row(
        session,
        org_id=org_id,
        account_id=account_id,
        pipeline_id=pipeline_id,
        node_id=node_id,
        name=name,
        eval_type=eval_type,
        config_json=config_json,
        pass_threshold=pass_threshold,
        suite_id=suite_id,
        eval_suite_id=eval_suite_id,
        existing_eval_id=existing_eval_id,
    )

    # --- PolicyGate persistence (branch 2 only) ---
    await _upsert_policy_gate(
        session,
        org_id=org_id,
        node_id=node_id,
        eval_type=eval_type,
        eval_id=eval_row.id,
        pg_action=pg_action,
        evaluation_mode=evaluation_mode,
    )
    return eval_row


def _resolve_pg_action(
    *,
    eval_type: str,
    org_id: uuid.UUID,
    node_id: uuid.UUID | None,
    existing_eval_id: uuid.UUID | None,
    failure_behaviour: str,
    config_json: dict[str, Any],
) -> str | None:
    """Validate the binding branch and return the PolicyGate action (or None)."""
    if eval_type == "guardrail":
        # Branch 1: guardrail — config-vocabulary validator only, no PolicyGate
        validate_guardrail_request(
            eval_type=eval_type,
            config_json=config_json,
        )
        _reject_unbound_output_point_guardrail(node_id=node_id, config_json=config_json)
        return None

    if node_id is not None:
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
        return failure_behaviour or "warn"

    # Branch 3: suite-scoped non-guardrail — no PolicyGate
    return None


async def _persist_eval_row(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    account_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    node_id: uuid.UUID | None,
    name: str,
    eval_type: str,
    config_json: dict[str, Any],
    pass_threshold: float | None,
    suite_id: str | None,
    eval_suite_id: uuid.UUID | None,
    existing_eval_id: uuid.UUID | None,
) -> Any:
    """Persist the Eval row (update path when ``existing_eval_id`` is given)."""
    from modulo.db.models.eval import Eval

    if existing_eval_id is not None:
        existing = (
            await session.execute(
                select(Eval).where(
                    Eval.id == existing_eval_id,
                    Eval.organisation_id == org_id,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            _apply_eval_update(
                existing,
                pipeline_id=pipeline_id,
                node_id=node_id,
                name=name,
                eval_type=eval_type,
                config_json=config_json,
                pass_threshold=pass_threshold,
                suite_id=suite_id,
                eval_suite_id=eval_suite_id,
            )
            await session.flush()
            return existing
        row = _build_eval_row(
            Eval,
            eval_id=existing_eval_id,
            org_id=org_id,
            account_id=account_id,
            pipeline_id=pipeline_id,
            node_id=node_id,
            name=name,
            eval_type=eval_type,
            config_json=config_json,
            pass_threshold=pass_threshold,
            suite_id=suite_id,
            eval_suite_id=eval_suite_id,
        )
    else:
        row = _build_eval_row(
            Eval,
            eval_id=None,
            org_id=org_id,
            account_id=account_id,
            pipeline_id=pipeline_id,
            node_id=node_id,
            name=name,
            eval_type=eval_type,
            config_json=config_json,
            pass_threshold=pass_threshold,
            suite_id=suite_id,
            eval_suite_id=eval_suite_id,
        )
    session.add(row)
    await session.flush()
    return row


def _apply_eval_update(
    existing: Any,
    *,
    pipeline_id: uuid.UUID,
    node_id: uuid.UUID | None,
    name: str,
    eval_type: str,
    config_json: dict[str, Any],
    pass_threshold: float | None,
    suite_id: str | None,
    eval_suite_id: uuid.UUID | None,
) -> None:
    """Apply the UPDATE path's field writes to an existing Eval row."""
    # Eval row exists (backfilled) — update it
    existing.pre_version_raw = {"config_json": existing.config_json}
    existing.version = (existing.version or 1) + 1
    existing.pipeline_id = pipeline_id
    existing.node_id = node_id
    existing.name = name
    existing.eval_type = eval_type
    existing.config_json = config_json
    existing.pass_threshold = pass_threshold
    existing.suite_id = suite_id
    if eval_suite_id is not None:
        existing.eval_suite_id = eval_suite_id


def _build_eval_row(
    eval_model: Any,
    *,
    eval_id: uuid.UUID | None,
    org_id: uuid.UUID,
    account_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    node_id: uuid.UUID | None,
    name: str,
    eval_type: str,
    config_json: dict[str, Any],
    pass_threshold: float | None,
    suite_id: str | None,
    eval_suite_id: uuid.UUID | None,
) -> Any:
    """Construct a new Eval row (version=1)."""
    fields: dict[str, Any] = {
        "organisation_id": org_id,
        "pipeline_id": pipeline_id,
        "node_id": node_id,
        "name": name,
        "eval_type": eval_type,
        "config_json": config_json,
        "pass_threshold": pass_threshold,
        "suite_id": suite_id,
        "eval_suite_id": eval_suite_id,
        "account_id": account_id,
        "version": 1,
    }
    if eval_id is not None:
        fields["id"] = eval_id
    return eval_model(**fields)


async def _upsert_policy_gate(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    node_id: uuid.UUID | None,
    eval_type: str,
    eval_id: Any,
    pg_action: str | None,
    evaluation_mode: str | None,
) -> None:
    """Upsert the PolicyGate row for branch 2 (no-op for other branches)."""
    if pg_action is None or node_id is None or eval_type == "guardrail":
        return

    from modulo.db.models.policy_gate import PolicyGate

    existing_gate = (
        await session.execute(
            select(PolicyGate).where(
                PolicyGate.eval_id == eval_id,
                PolicyGate.organisation_id == org_id,
                PolicyGate.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()

    if existing_gate is not None:
        _apply_gate_update(existing_gate, node_id=node_id, pg_action=pg_action, evaluation_mode=evaluation_mode)
    else:
        session.add(
            PolicyGate(
                organisation_id=org_id,
                eval_id=eval_id,
                node_id=node_id,
                action=pg_action,
                evaluation_mode=evaluation_mode or "automatic",
                version=1,
            )
        )

    await session.flush()


def _apply_gate_update(
    existing_gate: Any,
    *,
    node_id: uuid.UUID,
    pg_action: str,
    evaluation_mode: str | None,
) -> None:
    """Apply the UPDATE path's field writes to an existing PolicyGate row."""
    existing_gate.action = pg_action
    existing_gate.node_id = node_id
    # FAR-939 chunk 12 §3.1: ``None`` means *preserve the live mode* — an
    # update surface that does not carry the field (the MCP write seam) must
    # never reset a human-approval gate to automatic. Callers that carry it
    # (the REST update route) pass the resolved live value, so an explicit
    # write lands.
    if evaluation_mode is not None:
        existing_gate.evaluation_mode = evaluation_mode
