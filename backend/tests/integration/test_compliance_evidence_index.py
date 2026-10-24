"""Integration: the compliance evidence index over a real Postgres org (FAR-2036).

The unit-level suite runs the same queries against in-memory SQLite. This one
proves them on Postgres - the production dialect - including the aggregate
shapes, the ``DISTINCT`` snapshot join, and the tenant filter, over a seeded
organisation with real rows.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.compliance.artifacts import (
    ARTIFACT_AUDIT_CHAIN,
    ARTIFACT_EVAL_RESULT,
    ARTIFACT_HITL_DECISION,
    ARTIFACT_RUN_SNAPSHOT,
    INDEXED_ARTIFACT_TYPES,
)
from modulo.compliance.evidence_index import (
    AUTONOMY_DEMOTION_EVENT,
    AUTONOMY_PROMOTION_EVENT,
    STATUS_ABSENT,
    EvidenceIndexIncompleteError,
    build_evidence_index,
)
from modulo.compliance.pack import load_mapping_pack
from modulo.core.eval_engine import EvalType
from modulo.db.models.account import Account
from modulo.db.models.audit_event import AuditChainHead, AuditEvent
from modulo.db.models.eval import Eval
from modulo.db.models.eval_result import EvalResult
from modulo.db.models.hitl_claim import HitlClaim
from modulo.db.models.organisation import Organisation
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.pipeline_snapshot import PipelineSnapshot
from modulo.db.models.run import Run

pytestmark = pytest.mark.integration

_BASE_TIME = datetime(2026, 1, 15, 9, 0, 0, tzinfo=UTC)


async def _seed_org(session: AsyncSession, slug: str) -> dict[str, uuid.UUID]:
    """Seed one organisation with every artefact type the index maps."""
    org_id = uuid.uuid4()
    account_id = uuid.uuid4()
    pipeline_id = uuid.uuid4()
    snapshot_id = uuid.uuid4()
    run_id = uuid.uuid4()
    eval_id = uuid.uuid4()

    session.add(Organisation(id=org_id, name=f"Compliance {slug}", slug=slug))
    session.add(Account(id=account_id, email=f"admin-{slug}@compliance.example", display_name="Admin"))
    await session.flush()

    session.add(Pipeline(id=pipeline_id, organisation_id=org_id, name=f"{slug} pipeline", account_id=account_id))
    await session.flush()

    session.add(
        PipelineSnapshot(
            id=snapshot_id,
            organisation_id=org_id,
            pipeline_id=pipeline_id,
            snapshot_version=1,
            graph_json={"nodes": []},
            connector_bindings_json=[],
            schema_pins_json=[],
            prompt_pins_json=[],
            model_backend_pins_json=[],
            config_json={},
            run_context_defaults={},
        )
    )
    await session.flush()

    session.add(
        Run(
            id=run_id,
            organisation_id=org_id,
            pipeline_id=pipeline_id,
            snapshot_id=snapshot_id,
            trigger_type="manual",
            status="complete",
            run_number=1,
            input_hash="a" * 64,
            langgraph_thread_id=f"thread-{uuid.uuid4().hex}",
        )
    )
    await session.flush()

    for index, (decision, human_only) in enumerate(
        (("approved", True), ("approved", True), ("rejected", True), ("approved", False))
    ):
        session.add(
            HitlClaim(
                id=uuid.uuid4(),
                organisation_id=org_id,
                run_id=run_id,
                review_id=f"review-{slug}-{index}",
                pipeline_id=pipeline_id,
                expires_at=_BASE_TIME + timedelta(days=1),
                decision=decision,
                decision_at=_BASE_TIME + timedelta(hours=index + 1),
                decided_by=account_id,
                gate_config_json={"human_only": human_only},
            )
        )

    session.add(
        Eval(
            id=eval_id,
            organisation_id=org_id,
            pipeline_id=pipeline_id,
            account_id=account_id,
            eval_type=EvalType.MODEL_JUDGE,
            config_json={},
        )
    )
    await session.flush()
    for index, passed in enumerate((True, True, False)):
        session.add(
            EvalResult(
                id=uuid.uuid4(),
                organisation_id=org_id,
                eval_id=eval_id,
                run_id=run_id,
                passed=passed,
                outcome="pass" if passed else "fail",
                observed=True,
                evaluated_at=_BASE_TIME + timedelta(hours=index + 5),
            )
        )

    first_event = AuditEvent(
        id=uuid.uuid4(),
        organisation_id=org_id,
        event_type="pipeline_created",
        payload_json={},
        created_at=_BASE_TIME,
    )
    session.add(first_event)
    session.add_all(
        [
            AuditEvent(
                id=uuid.uuid4(),
                organisation_id=org_id,
                event_type=AUTONOMY_PROMOTION_EVENT,
                payload_json={},
                created_at=_BASE_TIME + timedelta(hours=8),
            ),
            AuditEvent(
                id=uuid.uuid4(),
                organisation_id=org_id,
                event_type=AUTONOMY_DEMOTION_EVENT,
                payload_json={},
                created_at=_BASE_TIME + timedelta(hours=9),
            ),
        ]
    )
    await session.flush()
    session.add(
        AuditChainHead(
            id=uuid.uuid4(),
            organisation_id=org_id,
            last_event_hash="f" * 64,
            last_event_id=first_event.id,
            event_count=3,
        )
    )
    await session.commit()

    return {"org_id": org_id, "snapshot_id": snapshot_id}


@pytest.mark.asyncio
async def test_seeded_org_index_includes_every_mapped_artefact_type(
    db_session: AsyncSession,
) -> None:
    seeded = await _seed_org(db_session, f"full-{uuid.uuid4().hex[:8]}")

    index = await build_evidence_index(db_session, seeded["org_id"])
    entries = {entry.artifact_type: entry for entry in index.artifacts}

    assert tuple(entries) == INDEXED_ARTIFACT_TYPES
    assert index.complete
    assert entries[ARTIFACT_AUDIT_CHAIN].record_count == 3
    assert entries[ARTIFACT_RUN_SNAPSHOT].record_count == 1
    assert entries[ARTIFACT_RUN_SNAPSHOT].identifiers == (f"pipeline-snapshot:{seeded['snapshot_id']}@v1",)
    assert entries[ARTIFACT_HITL_DECISION].record_count == 4
    assert entries[ARTIFACT_HITL_DECISION].detail["human_only_gate_decisions"] == 3
    assert entries[ARTIFACT_HITL_DECISION].detail["human_only_gate_approvals"] == 2
    assert entries[ARTIFACT_EVAL_RESULT].record_count == 3
    assert entries[ARTIFACT_EVAL_RESULT].detail["passed"] == 2
    assert entries[ARTIFACT_EVAL_RESULT].detail["failed"] == 1
    assert entries["autonomy_change"].record_count == 2
    assert index.pack_version == load_mapping_pack().pack_version


@pytest.mark.asyncio
async def test_empty_org_is_incomplete_and_strict_mode_raises(
    db_session: AsyncSession,
) -> None:
    org_id = uuid.uuid4()
    db_session.add(Organisation(id=org_id, name="Empty Compliance Org", slug=f"empty-{uuid.uuid4().hex[:8]}"))
    await db_session.commit()

    index = await build_evidence_index(db_session, org_id)

    assert not index.complete
    assert ARTIFACT_AUDIT_CHAIN in index.missing_artifact_types
    assert ARTIFACT_HITL_DECISION in index.missing_artifact_types
    for entry in index.artifacts:
        if entry.status == STATUS_ABSENT:
            assert entry.absence_reason

    with pytest.raises(EvidenceIndexIncompleteError):
        await build_evidence_index(db_session, org_id, strict=True)


@pytest.mark.asyncio
async def test_index_does_not_include_another_orgs_artefacts(db_session: AsyncSession) -> None:
    mine = await _seed_org(db_session, f"mine-{uuid.uuid4().hex[:8]}")
    await _seed_org(db_session, f"other-{uuid.uuid4().hex[:8]}")

    index = await build_evidence_index(db_session, mine["org_id"])

    assert index.organisation_id == str(mine["org_id"])
    assert {entry.artifact_type: entry for entry in index.artifacts}[ARTIFACT_HITL_DECISION].record_count == 4
