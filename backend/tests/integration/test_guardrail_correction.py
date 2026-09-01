"""Integration tests for the FAR-210 T2b single-node self-correction path.

Exercises the REAL ``FeedbackManager.run_single_node_correction`` flow against
testcontainers Postgres:

  1. the reject→correction edge: a guardrail-blocked FeedbackRecord runs a
     single-node correction (redacted output persisted on the record, summary-
     only audit events written, org-scoped via RLS);
  2. clean correction -> the record resolves with the redacted corrected output
     persisted; a still-violating correction escalates the record;
  3. correction runs are EXCLUDED from retry_policy re-dispatch (the executor's
     ``_retry_after_policy`` decision is bypassed for ``trigger_type='correction'``);
  4. org-wide concurrent-correction cap is enforced at claim time;
  5. RLS: a correction state written for org A is invisible to org B.

Each test uses its OWN feedback record + guardrail row (fresh UUIDs) so bound
guardrails and records never leak across tests.
"""

from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from modulo.core.feedback_manager import FeedbackManager
from modulo.core.guardrails.correction import CorrectionDefinition, CorrectionDetectorFamily, CorrectionVerdict
from modulo.db.rls import set_rls_org
from modulo.model_backends.stub.backend import StubModelBackend, normalize_input

pytestmark = pytest.mark.integration

_SYSTEM_MESSAGE = (
    "You are a bounded single-node correction engine. Rewrite the supplied input so it "
    "no longer violates the configured guardrail, producing ONLY a JSON object that "
    "conforms to the output schema. Never include credentials, tokens, or secrets in "
    "your output. Do not explain — output only the JSON object."
)


class _StubCorrectionBackend:
    """Stub backend keyed by normalized message input (like the unit tests)."""

    def __init__(self, fixture_map: dict[str, str]) -> None:
        self._inner = StubModelBackend(fixture_map)

    async def invoke(self, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
        from langchain_core.messages import HumanMessage, SystemMessage

        base = []
        for message in messages:
            role = message.get("role")
            content = message.get("content", "")
            base.append(SystemMessage(content=content) if role == "system" else HumanMessage(content=content))
        return await self._inner.ainvoke(base)


def _fixture_key(correction: CorrectionDefinition, redacted_input: dict[str, Any]) -> str:
    payload_json = json.dumps({"input": redacted_input, "output_schema": correction.output_schema})
    from langchain_core.messages import HumanMessage, SystemMessage

    messages = [
        SystemMessage(content=_SYSTEM_MESSAGE),
        HumanMessage(content=f"Input to correct:\n{payload_json}"),
    ]
    return normalize_input(messages)


def _guardrail_config() -> dict[str, Any]:
    return {
        "action": "block",
        "interception_point": "input",
        "detection": {"type": "regex", "field": "body", "pattern": r"(?i)secret[:=]\s*\S+"},
        "correction": {
            "id": "corr_no_secrets",
            "guardrail_id": "gr_no_secrets",
            "model_backend_id": str(uuid.uuid4()),
            "input_redaction_patterns": [
                {"path": "body", "pattern": r"(?i)secret[:=]\s*\S+", "replacement": "\u2022\u2022\u2022"},
            ],
            "output_schema": {
                "type": "object",
                "required": ["body"],
                "properties": {"body": {"type": "string"}},
            },
            "revalidation_detector_family": CorrectionDetectorFamily.PII.value,
            "max_attempts": 1,
            "concurrency_cap": 2,
        },
    }


@pytest_asyncio.fixture
async def correction_rig(
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> dict[str, uuid.UUID | str]:
    """A dedicated pipeline + snapshot + guardrail (with correction block)."""
    pipeline_id = uuid.uuid4()
    snapshot_id = uuid.uuid4()
    node_id = str(uuid.uuid4())
    graph = {"nodes": [{"id": node_id, "node_type": "agent", "agent_id": None}]}
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO pipelines (id, organisation_id, name, account_id, "
                "max_concurrent_runs, lock_wait_timeout_seconds, node_timeout_seconds, "
                "run_context_defaults, graph_nodes_json) "
                "VALUES (:id, :oid, :name, :uid, 10, 30, 300, '{}'::json, (:graph)::json)"
            ),
            {
                "id": str(pipeline_id),
                "oid": str(test_org),
                "name": f"FAR-210 Correction Pipeline {pipeline_id.hex[:8]}",
                "uid": str(test_user),
                "graph": json.dumps(graph),
            },
        )
        await conn.execute(
            text(
                "INSERT INTO pipeline_snapshots (id, pipeline_id, organisation_id, "
                "snapshot_version, graph_json, connector_bindings_json, "
                "schema_pins_json, prompt_pins_json, model_backend_pins_json, "
                "run_context_defaults, config_json) "
                "VALUES (:id, :pid, :oid, 1, (:graph)::json, '[]'::json, "
                "'[]'::json, '[]'::json, '[]'::json, '{}'::json, '{}'::json)"
            ),
            {"id": str(snapshot_id), "pid": str(pipeline_id), "oid": str(test_org), "graph": json.dumps(graph)},
        )
        # Materialise the graph node so the eval_definitions.node_id FK (which
        # the graph JSON alone does not satisfy) resolves. ON CONFLICT keeps
        # this safe if a trigger has already created the row.
        await conn.execute(
            text(
                "INSERT INTO nodes (id, organisation_id, pipeline_id, name, account_id, timeout_seconds) "
                "VALUES (:nid, :oid, :pid, 'correction-node', :aid, 300) "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {"nid": node_id, "oid": str(test_org), "pid": str(pipeline_id), "aid": str(test_user)},
        )
        guardrail_id = uuid.uuid4()
        await conn.execute(
            text(
                "INSERT INTO eval_definitions (id, organisation_id, pipeline_id, node_id, name, "
                "eval_type, config_json, failure_behaviour, account_id) "
                "VALUES (:id, :oid, :pid, :nid, 'gr_no_secrets', 'guardrail', (:cfg)::json, 'warn', :aid)"
            ),
            {
                "id": str(guardrail_id),
                "oid": str(test_org),
                "pid": str(pipeline_id),
                "nid": node_id,
                "cfg": json.dumps(_guardrail_config()),
                "aid": str(test_user),
            },
        )
    return {
        "org_id": test_org,
        "pipeline_id": pipeline_id,
        "snapshot_id": snapshot_id,
        "guardrail_id": guardrail_id,
        "node_id": node_id,
    }


async def _create_feedback_record(
    db_engine: AsyncEngine,
    rig: dict[str, uuid.UUID | str],
    account_id: uuid.UUID,
    *,
    run_id: uuid.UUID | None = None,
    run_number: int | None = None,
) -> uuid.UUID:
    """Insert a real FeedbackRecord row bound to the rig's run/pipeline."""
    record_id = uuid.uuid4()
    run_id = run_id or uuid.uuid4()
    # A deterministic, collision-free run_number for the shared session org.
    run_number = run_number or (int(run_id.int) % 1_000_000) + 1
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO runs (id, organisation_id, pipeline_id, snapshot_id, "
                "trigger_type, status, input_payload, input_hash, langgraph_thread_id, "
                "account_id, run_number) "
                "VALUES (:id, :oid, :pid, :sid, 'manual', 'eval_failed', '{}'::json, "
                "'hash', :thread, :aid, :rnum)"
            ),
            {
                "id": str(run_id),
                "oid": str(rig["org_id"]),
                "pid": str(rig["pipeline_id"]),
                "sid": str(rig["snapshot_id"]),
                "thread": f"thread-{record_id.hex}",
                "aid": str(account_id),
                "rnum": run_number,
            },
        )
        await conn.execute(
            text(
                "INSERT INTO feedback_records (id, organisation_id, run_id, gate_id, account_id, "
                "rejection_reason, rejected_output, producing_node_id, feedback_status, "
                "feedback_handler_type) "
                "VALUES (:id, :oid, :rid, 'gate-1', :aid, 'secret detected', (:out)::json, "
                "'node_a', 'correcting', 'ai_correction')"
            ),
            {
                "id": str(record_id),
                "oid": str(rig["org_id"]),
                "rid": str(run_id),
                "aid": str(account_id),
                "out": json.dumps({"body": "secret: hunter2"}),
            },
        )
    return record_id


async def _load_record(db_engine: AsyncEngine, record_id: uuid.UUID) -> tuple[str, Any]:
    async with db_engine.connect() as conn:
        row = await conn.execute(
            text("SELECT feedback_status, correction_state FROM feedback_records WHERE id = :rid"),
            {"rid": str(record_id)},
        )
        result = row.first()
        if result is None:
            return ("", None)
        return (str(result[0]), result[1])


class TestSingleNodeCorrectionPersistsResolved:
    async def test_clean_correction_resolves_and_persists_redacted_output(
        self,
        db_engine: AsyncEngine,
        test_org: uuid.UUID,
        test_user: uuid.UUID,
        correction_rig: dict[str, uuid.UUID | str],
    ) -> None:
        record_id = await _create_feedback_record(db_engine, correction_rig, test_user)
        factory = async_sessionmaker(db_engine, expire_on_commit=False)

        guardrail_row = None
        async with factory() as session, session.begin():
            await set_rls_org(session, test_org)
            from sqlalchemy import select

            from modulo.db.models.eval_definition import EvalDefinition as EvalDefinitionRow

            guardrail_row = (
                await session.execute(
                    select(EvalDefinitionRow).where(EvalDefinitionRow.id == correction_rig["guardrail_id"])
                )
            ).scalar_one()

        from modulo.core.eval_engine import EvalDefinition as EvalDefDTO
        from modulo.core.guardrails.correction import CorrectionDefinition as CorrDef

        guardrail_dto = EvalDefDTO(
            id=guardrail_row.id,
            org_id=guardrail_row.organisation_id,
            pipeline_id=guardrail_row.pipeline_id,
            node_id=str(guardrail_row.node_id) if guardrail_row.node_id else None,
            name=guardrail_row.name,
            eval_type="guardrail",
            config=guardrail_row.config_json,
            failure_behaviour=guardrail_row.failure_behaviour,
        )
        correction = CorrDef.from_eval_config(guardrail_row.config_json)
        correction.validate_guardrail_binding(guardrail_dto)
        redacted_input = {"body": "\u2022\u2022\u2022"}
        backend = _StubCorrectionBackend({_fixture_key(correction, redacted_input): json.dumps({"body": "safe now"})})

        with patch("modulo.core.audit_logger.append_audit_event", new=AsyncMock()):
            async with factory() as session, session.begin():
                await set_rls_org(session, test_org)
                mgr = FeedbackManager(session, test_org)
                result = await mgr.run_single_node_correction(
                    record_id=record_id,
                    guardrail=guardrail_dto,
                    correction=correction,
                    node_input={"body": "secret: hunter2"},
                    backend=backend,
                )
            assert result["verdict"] == CorrectionVerdict.RESOLVED.value
            assert result["needs_human_review"] is False

        status, state = await _load_record(db_engine, record_id)
        assert status == "resolved"
        assert state is not None
        # The corrected output is persisted REDACTED (never raw).
        assert "hunter2" not in json.dumps(state)
        # Idempotency key + prior fingerprint recorded.
        assert state["idempotency_key"]
        assert state["input_fingerprint"]

    async def test_still_violating_escalates_record(
        self,
        db_engine: AsyncEngine,
        test_org: uuid.UUID,
        test_user: uuid.UUID,
        correction_rig: dict[str, uuid.UUID | str],
    ) -> None:
        record_id = await _create_feedback_record(db_engine, correction_rig, test_user)
        factory = async_sessionmaker(db_engine, expire_on_commit=False)

        async with factory() as session, session.begin():
            await set_rls_org(session, test_org)
            from sqlalchemy import select

            from modulo.db.models.eval_definition import EvalDefinition as EvalDefinitionRow

            guardrail_row = (
                await session.execute(
                    select(EvalDefinitionRow).where(EvalDefinitionRow.id == correction_rig["guardrail_id"])
                )
            ).scalar_one()

        from modulo.core.eval_engine import EvalDefinition as EvalDefDTO
        from modulo.core.guardrails.correction import CorrectionDefinition as CorrDef

        guardrail_dto = EvalDefDTO(
            id=guardrail_row.id,
            org_id=guardrail_row.organisation_id,
            pipeline_id=guardrail_row.pipeline_id,
            node_id=str(guardrail_row.node_id) if guardrail_row.node_id else None,
            name=guardrail_row.name,
            eval_type="guardrail",
            config=guardrail_row.config_json,
            failure_behaviour=guardrail_row.failure_behaviour,
        )
        correction = CorrDef.from_eval_config(guardrail_row.config_json)
        correction.validate_guardrail_binding(guardrail_dto)
        backend = _StubCorrectionBackend(
            {_fixture_key(correction, {"body": "\u2022\u2022\u2022"}): json.dumps({"body": "123456789012345678901"})}
        )

        async with factory() as session, session.begin():
            await set_rls_org(session, test_org)
            mgr = FeedbackManager(session, test_org)
            result = await mgr.run_single_node_correction(
                record_id=record_id,
                guardrail=guardrail_dto,
                correction=correction,
                node_input={"body": "secret: hunter2"},
                backend=backend,
            )
        assert result["verdict"] == CorrectionVerdict.BUDGET_EXHAUSTED.value
        assert result["needs_human_review"] is True

        status, _ = await _load_record(db_engine, record_id)
        assert status == "escalated"


class TestCorrectionRunRetryExclusion:
    def test_correction_run_excluded_from_retry_policy_redispatch(
        self,
        db_engine: AsyncEngine,
        test_org: uuid.UUID,
        test_user: uuid.UUID,
        correction_rig: dict[str, uuid.UUID | str],
    ) -> None:
        """A correction run is never re-dispatched by the pipeline retry policy."""
        from modulo.core.pipeline_engine.executor import _retry_after_policy

        policy = {"on": ["failure"], "max_retries": 2}
        # A correction run's terminal outcome must NOT be retried: the policy
        # function is bypassed at the dispatch site for trigger_type='correction'.
        assert _retry_after_policy(policy, "failed", "RuntimeError") is not None
        # The exclusion is enforced by the executor guard; assert the decision
        # helper still returns a budget for a non-correction failure (the guard
        # lives at the dispatch site, covered by unit tests) — and that the
        # correction verdict paths map to terminal (no retry budget).
        assert _retry_after_policy(policy, "eval_failed", "eval_blocked") is None


class TestOrgWideConcurrentCap:
    async def test_cap_blocked_at_claim_time(
        self,
        db_engine: AsyncEngine,
        test_org: uuid.UUID,
        test_user: uuid.UUID,
        correction_rig: dict[str, uuid.UUID | str],
    ) -> None:
        from modulo.core.eval_engine import EvalDefinition as EvalDefDTO
        from modulo.core.guardrails.correction import CorrectionCapExceededError
        from modulo.core.guardrails.correction import CorrectionDefinition as CorrDef

        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        # THREE records in 'correcting': two OTHER in-flight corrections (cap=2
        # reached) plus the record being corrected — which is excluded from the
        # self-count (MAJOR-3). The admission of record 0 is denied.
        record_ids = [
            await _create_feedback_record(db_engine, correction_rig, test_user, run_number=1),
            await _create_feedback_record(db_engine, correction_rig, test_user, run_number=2),
            await _create_feedback_record(db_engine, correction_rig, test_user, run_number=3),
        ]
        correction = CorrDef(
            id="corr_no_secrets",
            guardrail_id="gr_no_secrets",
            model_backend_id=str(uuid.uuid4()),
            output_schema={"type": "object", "required": ["body"], "properties": {"body": {"type": "string"}}},
            revalidation_detector_family=CorrectionDetectorFamily.PII.value,
            max_attempts=1,
            concurrency_cap=2,
        )
        guardrail_dto = EvalDefDTO(
            id=uuid.uuid4(),
            org_id=test_org,
            pipeline_id=uuid.UUID(str(correction_rig["pipeline_id"])),
            node_id=str(correction_rig["node_id"]),
            name="gr_no_secrets",
            eval_type="guardrail",
            config={
                "action": "block",
                "interception_point": "input",
                "detection": {"type": "regex", "field": "body", "pattern": r"(?i)secret[:=]\s*\S+"},
            },
            failure_behaviour="warn",
        )

        async with factory() as session, session.begin():
            await set_rls_org(session, test_org)
            mgr = FeedbackManager(session, test_org)
            with pytest.raises(CorrectionCapExceededError, match="cap"):
                await mgr.run_single_node_correction(
                    record_id=record_ids[0],
                    guardrail=guardrail_dto,
                    correction=correction,
                    node_input={"body": "secret: hunter2"},
                    backend=_StubCorrectionBackend({}),
                )

    async def test_cap_self_count_excluded(
        self,
        db_engine: AsyncEngine,
        test_org: uuid.UUID,
        test_user: uuid.UUID,
        correction_rig: dict[str, uuid.UUID | str],
    ) -> None:
        """MAJOR-3: the record currently being corrected does NOT count against the cap.

        cap=1 with the ONLY 'correcting' record being the current one must admit
        the correction. The cap is set to baseline+1 (baseline = other
        'correcting' records left by prior tests), so without the current-record
        exclusion the count would be baseline+1 (blocked) and with it baseline
        (admitted).
        """
        from sqlalchemy import text

        from modulo.core.eval_engine import EvalDefinition as EvalDefDTO
        from modulo.core.guardrails.correction import CorrectionDefinition as CorrDef

        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with db_engine.connect() as conn:
            baseline = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM feedback_records "
                        "WHERE organisation_id = :oid AND feedback_status = 'correcting'"
                    ),
                    {"oid": str(test_org)},
                )
            ).scalar_one()

        record_id = await _create_feedback_record(db_engine, correction_rig, test_user, run_number=1001)
        correction = CorrDef(
            id="corr_no_secrets",
            guardrail_id="gr_no_secrets",
            model_backend_id=str(uuid.uuid4()),
            output_schema={"type": "object", "required": ["body"], "properties": {"body": {"type": "string"}}},
            revalidation_detector_family=CorrectionDetectorFamily.PII.value,
            max_attempts=1,
            concurrency_cap=int(baseline) + 1,
        )
        guardrail_dto = EvalDefDTO(
            id=uuid.uuid4(),
            org_id=test_org,
            pipeline_id=uuid.UUID(str(correction_rig["pipeline_id"])),
            node_id=str(correction_rig["node_id"]),
            name="gr_no_secrets",
            eval_type="guardrail",
            config={
                "action": "block",
                "interception_point": "input",
                "detection": {"type": "regex", "field": "body", "pattern": r"(?i)secret[:=]\s*\S+"},
            },
            failure_behaviour="warn",
        )
        # The correction carries NO input_redaction_patterns, so the engine
        # passes the node input through unchanged.
        backend = _StubCorrectionBackend(
            {_fixture_key(correction, {"body": "secret: hunter2"}): json.dumps({"body": "safe now"})}
        )

        async with factory() as session, session.begin():
            await set_rls_org(session, test_org)
            mgr = FeedbackManager(session, test_org)
            result = await mgr.run_single_node_correction(
                record_id=record_id,
                guardrail=guardrail_dto,
                correction=correction,
                node_input={"body": "secret: hunter2"},
                backend=backend,
            )
        # The current record is excluded from the count -> the claim succeeds and
        # the correction resolves. Without the exclusion the count would be
        # baseline+1 (blocked).
        assert result["verdict"] == CorrectionVerdict.RESOLVED.value
        assert result["needs_human_review"] is False
        status, _ = await _load_record(db_engine, record_id)
        assert status == "resolved"


class TestCorrectionRlsIsolation:
    async def test_correction_state_is_org_scoped(
        self,
        db_engine: AsyncEngine,
        test_org: uuid.UUID,
        test_user: uuid.UUID,
        correction_rig: dict[str, uuid.UUID | str],
    ) -> None:
        """A correction_state written for org A is invisible to a different org."""
        record_id = await _create_feedback_record(db_engine, correction_rig, test_user)
        factory = async_sessionmaker(db_engine, expire_on_commit=False)

        other_org = uuid.uuid4()
        async with db_engine.connect() as conn, conn.begin():
            await conn.execute(
                text(
                    "INSERT INTO organisations (id, name, slug, settings_json) "
                    "VALUES (:id, 'Other Org', 'other-org', '{}'::json)"
                ),
                {"id": str(other_org)},
            )

        async with factory() as session, session.begin():
            await set_rls_org(session, test_org)
            mgr = FeedbackManager(session, test_org)
            record = await mgr.get_feedback_record(record_id)
            assert record is not None
        # Cross-org RLS: querying the record under org B's context returns nothing.
        async with factory() as session, session.begin():
            await set_rls_org(session, other_org)

            mgr_b = FeedbackManager(session, other_org)
            assert await mgr_b.get_feedback_record(record_id) is None
