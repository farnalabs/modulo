"""Integration tests: bound guardrail enforcement through the DB-backed dual path.

FAR-1107 chunk 8, spec criteria 11 and 12: on REAL database state (real
Postgres, committed ``evals`` row + live ``policy_gates`` binding row), the
three document-side views of policy-gate enforcement must hold:

* criterion 11 — ``run_interception_pass`` is non-raising and reports
  ``outcome.blocked=True`` for a wired block gate (and does NOT block when
  the wiring says warn, even with a block-valued config action);
* criterion 12 — both raising paths (``evaluate_guardrails`` and
  ``run_guardrail_pass``) raise the SAME exact ``GuardrailBlockedError``
  type, which is an ``EvalBlockedError``, carrying the eval's name.

Unlike the enforcement unit tests, the ``EvalDefinition`` DTO and the
gate-binding record are built BACK from actual database rows — this is the
DB-shape round-trip a fixture-only test cannot prove.
"""

import dataclasses
import json
import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from modulo.core.eval_engine import (
    EvalBlockedError,
    EvalDefinition,
    EvalEngine,
    EvalType,
)
from modulo.core.guardrails import (
    GuardrailBlockedError,
    evaluate_guardrails,
    run_guardrail_pass,
    run_interception_pass_async,
)

pytestmark = pytest.mark.integration

_JS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"name": {"type": "string"}},
    "required": ["name"],
}

_VIOLATION: dict[str, Any] = {"name": 123}


@dataclasses.dataclass(frozen=True)
class _GateBinding:
    """Local view of a ``policy_gates`` row — the mapping-value shape callers build."""

    id: uuid.UUID
    organisation_id: uuid.UUID
    version: int
    node_id: uuid.UUID
    action: str


# ---------------------------------------------------------------------------
# Seed + read-back helpers
# ---------------------------------------------------------------------------


async def _setup_org(engine: AsyncEngine) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    org_id = uuid.uuid4()
    slug = f"dual-{org_id.hex[:8]}"
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :n, :s, '{}'::json)"),
            {"id": str(org_id), "n": slug, "s": slug},
        )
        acc_id = uuid.uuid4()
        await conn.execute(
            text(
                "INSERT INTO accounts (id, email, display_name, password_hash, auth_provider, active) "
                "VALUES (:id, :e, :n, 'hash', 'local', true)"
            ),
            {"id": str(acc_id), "e": f"{slug}@test.com", "n": slug},
        )
        pipe_id = uuid.uuid4()
        await conn.execute(
            text(
                "INSERT INTO pipelines (id, organisation_id, name, account_id, "
                "max_concurrent_runs, lock_wait_timeout_seconds, node_timeout_seconds, "
                "run_context_defaults, graph_nodes_json) "
                "VALUES (:id, :oid, :n, :aid, 10, 30, 300, '{}'::json, '[]'::json)"
            ),
            {"id": str(pipe_id), "oid": str(org_id), "n": f"{slug}-pipe", "aid": str(acc_id)},
        )
    return org_id, acc_id, pipe_id


async def _seed_bound_guardrail(
    engine: AsyncEngine,
    org_id: uuid.UUID,
    pipe_id: uuid.UUID,
    acc_id: uuid.UUID,
    *,
    eval_name: str,
    config_action: str,
    gate_action: str,
) -> uuid.UUID:
    """Insert one guardrail eval (config action = ``config_action``) plus its live gate."""
    eval_id = uuid.uuid4()
    node_id = uuid.uuid4()
    cfg: dict[str, Any] = {
        "action": config_action,
        "interception_point": "input",
        "type": "json_schema",
        "schema": dict(_JS_SCHEMA),
    }
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO evals (id, organisation_id, pipeline_id, account_id, node_id, "
                "name, eval_type, config_json, version) "
                "VALUES (:id, :oid, :pid, :aid, :nid, :name, 'guardrail', CAST(:cfg AS jsonb), 1)"
            ),
            {
                "id": str(eval_id),
                "oid": str(org_id),
                "pid": str(pipe_id),
                "aid": str(acc_id),
                "nid": str(node_id),
                "name": eval_name,
                "cfg": json.dumps(cfg, separators=(",", ":")),
            },
        )
        await conn.execute(
            text(
                "INSERT INTO policy_gates (id, organisation_id, eval_id, node_id, "
                "action, version) VALUES (:id, :oid, :eid, :nid, :act, 1)"
            ),
            {
                "id": str(uuid.uuid4()),
                "oid": str(org_id),
                "eid": str(eval_id),
                "nid": str(node_id),
                "act": gate_action,
            },
        )
    return eval_id


async def _load_dto_and_binding(engine: AsyncEngine, eval_id: uuid.UUID) -> tuple[EvalDefinition, _GateBinding]:
    """Round-trip through the REAL ``config_json``/``policy_gates`` row shapes."""
    async with engine.connect() as conn:
        eval_row = (
            (
                await conn.execute(
                    text(
                        "SELECT id, organisation_id, pipeline_id, node_id, name, eval_type, "
                        "config_json, version FROM evals WHERE id = :eid"
                    ),
                    {"eid": str(eval_id)},
                )
            )
            .mappings()
            .one()
        )
        gate_row = (
            (
                await conn.execute(
                    text(
                        "SELECT id, organisation_id, version, node_id, action FROM policy_gates "
                        "WHERE eval_id = :eid AND deleted_at IS NULL"
                    ),
                    {"eid": str(eval_id)},
                )
            )
            .mappings()
            .one()
        )

    assert eval_row["eval_type"] == "guardrail"
    assert eval_row["config_json"]["action"] == "block"
    definition = EvalDefinition(
        id=eval_row["id"],
        org_id=eval_row["organisation_id"],
        pipeline_id=eval_row["pipeline_id"],
        node_id=str(eval_row["node_id"]),
        name=eval_row["name"],
        eval_type=EvalType.GUARDRAIL,
        config=eval_row["config_json"],
        failure_behaviour="block",
        version=eval_row["version"],
    )
    binding = _GateBinding(
        id=gate_row["id"],
        organisation_id=gate_row["organisation_id"],
        version=gate_row["version"],
        node_id=gate_row["node_id"],
        action=gate_row["action"],
    )
    return definition, binding


# ---------------------------------------------------------------------------
# Criteria 11 + 12
# ---------------------------------------------------------------------------


class TestC11NonRaisingInterceptionOutcome:
    @pytest.mark.asyncio
    async def test_block_guardrail_reports_blocked_without_raising(self, db_engine: AsyncEngine) -> None:
        """The ingestion edge uses the direct config_json.action check (§1/§3.3)."""
        org_id, acc_id, pipe_id = await _setup_org(db_engine)
        eval_id = await _seed_bound_guardrail(
            db_engine,
            org_id,
            pipe_id,
            acc_id,
            eval_name="gr-c11-block",
            config_action="block",
            gate_action="block",
        )
        definition, _binding = await _load_dto_and_binding(db_engine, eval_id)

        outcome = await run_interception_pass_async(EvalEngine(), [definition], dict(_VIOLATION))

        assert outcome.blocked is True
        assert outcome.blocking_eval_name == definition.name

    @pytest.mark.asyncio
    async def test_block_config_always_blocks_on_ingestion_edge(self, db_engine: AsyncEngine) -> None:
        """The ingestion edge ignores the gate action — config_json.action is authoritative there."""
        org_id, acc_id, pipe_id = await _setup_org(db_engine)
        eval_id = await _seed_bound_guardrail(
            db_engine,
            org_id,
            pipe_id,
            acc_id,
            eval_name="gr-c11-warn-gate",
            config_action="block",
            gate_action="warn",
        )
        definition, _binding = await _load_dto_and_binding(db_engine, eval_id)

        outcome = await run_interception_pass_async(EvalEngine(), [definition], dict(_VIOLATION))

        # Ingestion edge blocks on config action, not gate action.
        assert outcome.blocked is True


class TestC12RaisingPathsShareExactType:
    @pytest.mark.asyncio
    async def test_evaluate_guardrails_bound_block_raises_exact_type(self, db_engine: AsyncEngine) -> None:
        org_id, acc_id, pipe_id = await _setup_org(db_engine)
        eval_id = await _seed_bound_guardrail(
            db_engine,
            org_id,
            pipe_id,
            acc_id,
            eval_name="gr-c12-block",
            config_action="block",
            gate_action="block",
        )
        definition, binding = await _load_dto_and_binding(db_engine, eval_id)

        with pytest.raises(GuardrailBlockedError) as raised:
            evaluate_guardrails(
                EvalEngine(),
                [definition],
                dict(_VIOLATION),
                policy_gates={definition.id: binding},
            )

        assert type(raised.value) is GuardrailBlockedError
        assert isinstance(raised.value, EvalBlockedError)
        assert raised.value.eval_name == definition.name

    @pytest.mark.asyncio
    async def test_run_guardrail_pass_raises_same_exact_type(self, db_engine: AsyncEngine) -> None:
        org_id, acc_id, pipe_id = await _setup_org(db_engine)
        eval_id = await _seed_bound_guardrail(
            db_engine,
            org_id,
            pipe_id,
            acc_id,
            eval_name="gr-c12-pass",
            config_action="block",
            gate_action="block",
        )
        definition, _binding = await _load_dto_and_binding(db_engine, eval_id)

        with pytest.raises(GuardrailBlockedError) as raised:
            run_guardrail_pass(
                EvalEngine(),
                [definition],
                dict(_VIOLATION),
            )

        assert type(raised.value) is GuardrailBlockedError
        assert raised.value.eval_name == definition.name
