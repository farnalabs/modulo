"""Integration tests for the feedback flow via FeedbackManager with real DB.

Uses the local conftest fixtures (test_org, test_user, rls_session) backed by
the session-scoped testcontainers Postgres from tests/integration/conftest.py.
All inserts are rolled back after each test.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.feedback_manager import FeedbackManager

pytestmark = pytest.mark.integration


@pytest.mark.usefixtures("rls_session")
class TestFeedbackFlowUnit:
    """Test FeedbackManager methods against a real DB session."""

    async def test_create_feedback_record_persists(
        self, rls_session: AsyncSession, test_org: uuid.UUID, test_user: uuid.UUID
    ) -> None:
        run_id = await _create_seed_run(rls_session, test_org, test_user)
        mgr = FeedbackManager(rls_session, test_org)

        record = await mgr.create_feedback_record(
            run_id=run_id,
            gate_id="gate-1",
            rejected_by=test_user,
            rejection_reason="Output quality insufficient",
            rejected_output={"result": "poor quality text"},
            producing_node_id="node-b",
            producing_agent_id=None,
            feedback_handler_type="human",
        )

        assert record.id is not None
        assert record.run_id == run_id
        assert record.feedback_status == "pending"
        assert record.rejection_reason == "Output quality insufficient"

    async def test_get_feedback_record_by_id(
        self, rls_session: AsyncSession, test_org: uuid.UUID, test_user: uuid.UUID
    ) -> None:
        run_id = await _create_seed_run(rls_session, test_org, test_user)
        mgr = FeedbackManager(rls_session, test_org)
        created = await mgr.create_feedback_record(
            run_id=run_id,
            gate_id="gate-2",
            rejected_by=test_user,
            rejection_reason="Bad",
            rejected_output={},
            producing_node_id="node-c",
        )

        fetched = await mgr.get_feedback_record(created.id)
        assert fetched is not None
        assert fetched.id == created.id
        assert fetched.feedback_status == "pending"

    async def test_get_feedback_record_not_found(
        self, rls_session: AsyncSession, test_org: uuid.UUID,
    ) -> None:
        mgr = FeedbackManager(rls_session, test_org)
        result = await mgr.get_feedback_record(uuid.uuid4())
        assert result is None

    async def test_list_feedback_records_pagination(
        self, rls_session: AsyncSession, test_org: uuid.UUID, test_user: uuid.UUID
    ) -> None:
        run_id = await _create_seed_run(rls_session, test_org, test_user)
        mgr = FeedbackManager(rls_session, test_org)

        for i in range(3):
            await mgr.create_feedback_record(
                run_id=run_id,
                gate_id=f"gate-{i}",
                rejected_by=test_user,
                rejection_reason=f"Reason {i}",
                rejected_output={},
                producing_node_id="node-b",
            )

        result = await mgr.get_feedback_records(page=1, page_size=2)
        assert result["total"] == 3
        assert len(result["items"]) == 2

        result_page2 = await mgr.get_feedback_records(page=2, page_size=2)
        assert len(result_page2["items"]) == 1

    async def test_list_feedback_records_empty(
        self, rls_session: AsyncSession, test_org: uuid.UUID,
    ) -> None:
        mgr = FeedbackManager(rls_session, test_org)
        result = await mgr.get_feedback_records()
        assert result["total"] == 0
        assert len(result["items"]) == 0

    async def test_update_status_transition(
        self, rls_session: AsyncSession, test_org: uuid.UUID, test_user: uuid.UUID
    ) -> None:
        run_id = await _create_seed_run(rls_session, test_org, test_user)
        mgr = FeedbackManager(rls_session, test_org)
        record = await mgr.create_feedback_record(
            run_id=run_id,
            gate_id="gate-1",
            rejected_by=test_user,
            rejection_reason="Needs correction",
            rejected_output={},
            producing_node_id="node-b",
        )

        updated = await mgr.update_status(record.id, "routing")
        assert updated is not None
        assert updated.feedback_status == "routing"

        escalated = await mgr.update_status(record.id, "escalated")
        assert escalated is not None
        assert escalated.feedback_status == "escalated"

    async def test_update_status_not_found(
        self, rls_session: AsyncSession, test_org: uuid.UUID,
    ) -> None:
        mgr = FeedbackManager(rls_session, test_org)
        result = await mgr.update_status(uuid.uuid4(), "resolved")
        assert result is None

    async def test_link_correction_run(
        self, rls_session: AsyncSession, test_org: uuid.UUID, test_user: uuid.UUID
    ) -> None:
        run_id = await _create_seed_run(rls_session, test_org, test_user)
        correction_id = await _create_seed_run(rls_session, test_org, test_user)
        mgr = FeedbackManager(rls_session, test_org)
        record = await mgr.create_feedback_record(
            run_id=run_id,
            gate_id="gate-1",
            rejected_by=test_user,
            rejection_reason="Fix it",
            rejected_output={},
            producing_node_id="node-b",
        )

        updated = await mgr.link_correction_run(record.id, correction_id)
        assert updated is not None
        assert updated.correction_run_id == correction_id
        assert updated.feedback_status == "correcting"

    async def test_create_human_review_feedback(
        self, rls_session: AsyncSession, test_org: uuid.UUID, test_user: uuid.UUID
    ) -> None:
        run_id = await _create_seed_run(rls_session, test_org, test_user)
        mgr = FeedbackManager(rls_session, test_org)

        record = await mgr.create_feedback_record(
            run_id=run_id,
            gate_id="gate-review",
            rejected_by=test_user,
            rejection_reason="Manual review required",
            rejected_output={"doc": "needs human edit"},
            producing_node_id="node-d",
            feedback_handler_type="ai_correction_with_human_review",
        )

        assert record.feedback_handler_type == "ai_correction_with_human_review"
        assert record.feedback_status == "correcting"

    async def test_create_ai_correction_feedback(
        self, rls_session: AsyncSession, test_org: uuid.UUID, test_user: uuid.UUID
    ) -> None:
        run_id = await _create_seed_run(rls_session, test_org, test_user)
        mgr = FeedbackManager(rls_session, test_org)

        record = await mgr.create_feedback_record(
            run_id=run_id,
            gate_id="gate-auto",
            rejected_by=test_user,
            rejection_reason="Auto-fix",
            rejected_output={"code": "buggy"},
            producing_node_id="node-e",
            feedback_handler_type="ai_correction",
        )

        assert record.feedback_handler_type == "ai_correction"
        assert record.rejected_output == {"code": "buggy"}

    async def test_filter_by_status(
        self, rls_session: AsyncSession, test_org: uuid.UUID, test_user: uuid.UUID
    ) -> None:
        run_id = await _create_seed_run(rls_session, test_org, test_user)
        mgr = FeedbackManager(rls_session, test_org)

        r1 = await mgr.create_feedback_record(
            run_id=run_id, gate_id="g1", rejected_by=test_user,
            rejection_reason="R1", rejected_output={}, producing_node_id="n1",
        )
        await mgr.create_feedback_record(
            run_id=run_id, gate_id="g2", rejected_by=test_user,
            rejection_reason="R2", rejected_output={}, producing_node_id="n2",
        )
        await mgr.update_status(r1.id, "routing")
        await mgr.update_status(r1.id, "resolved")

        pending_result = await mgr.get_feedback_records(status="pending")
        assert pending_result["total"] == 1

        resolved_result = await mgr.get_feedback_records(status="resolved")
        assert resolved_result["total"] == 1


async def _create_seed_run(
    session: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID,
) -> uuid.UUID:
    """Create a minimal run row needed for feedback FK constraints."""
    run_id = uuid.uuid4()
    thread_id = str(uuid.uuid4())
    pipeline_id = uuid.uuid4()
    snapshot_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO pipelines (id, organisation_id, name, created_by, run_context_defaults) "
            "VALUES (:id, :org_id, :name, :user_id, '{}'::json)"
        ),
        {
            "id": str(pipeline_id),
            "org_id": str(org_id),
            "name": "Feedback Test Pipeline",
            "user_id": str(user_id),
        },
    )
    await session.execute(
        text(
            "INSERT INTO pipeline_snapshots (id, pipeline_id, organisation_id, snapshot_version, graph_json, "
            "  config_json, created_by, connector_bindings_json, schema_pins_json, "
            "  prompt_pins_json, model_backend_pins_json, run_context_defaults) "
            "VALUES (:id, :pipeline_id, :org_id, 1, :graph, :config, :user_id, "
            "'[]'::json, '[]'::json, '[]'::json, '[]'::json, '{}'::json)"
        ),
        {
            "id": str(snapshot_id),
            "pipeline_id": str(pipeline_id),
            "org_id": str(org_id),
            "graph": '{"nodes": [], "edges": []}',
            "config": "{}",
            "user_id": str(user_id),
        },
    )
    await session.execute(
        text(
            "INSERT INTO runs (id, organisation_id, pipeline_id, snapshot_id, "
            "  trigger_type, status, input_hash, langgraph_thread_id, created_by) "
            "VALUES (:id, :org_id, :pipeline_id, :snapshot_id, "
            "  :trigger_type, :status, :input_hash, :thread_id, :created_by)"
        ),
        {
            "id": str(run_id),
            "org_id": str(org_id),
            "pipeline_id": str(pipeline_id),
            "snapshot_id": str(snapshot_id),
            "trigger_type": "manual",
            "status": "failed",
            "input_hash": "abc123",
            "thread_id": thread_id,
            "created_by": str(user_id),
        },
    )
    return run_id
